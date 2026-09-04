"""Per-agent worker running one PuffoCoreMessageClient loop.

Owns the agent's adapter + WS listen loop + heartbeat task; written
into runtime.json so the CLI can read live stats without IPC.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..agent.adapters import Adapter
from ..agent.core import AgentAPIError as AgentAPIError
from ..agent.core import PuffoAgent as PuffoAgent
from ..agent.provider_failures import provider_failure_message
from ..limits import (
    DEFAULT_CATCHUP_STALE_HOURS,
    MAX_INLINE_MESSAGE_CHARS,
    MESSAGE_SEGMENT_CHARS,
)
from ..agent.status_reporter import StatusReporter
from ..crypto.keystore import decode_secret
from ..crypto.primitives import Ed25519KeyPair
from .runtime_matrix import (
    HARNESS_PROVIDERS,
    RUNTIME_CLI_DOCKER,
    RUNTIME_CLI_LOCAL,
    RUNTIME_WS_LOCAL,
    resolve_effective_harness,
    resolve_effective_provider,
)
from .ws_local.hub import AttachPoint
from ..agent.shared_content import (
    looks_like_managed_claude_md as looks_like_managed_claude_md,
)
from ..agent.shared_content import rebuild_agent_claude_md, rebuild_agent_codex_md
from .state import (
    AgentConfig,
    DaemonConfig,
    RuntimeState,
    agent_claude_user_dir,
    agent_codex_user_dir,
    agent_dir,
    agent_home_dir,
    claude_cli_api_key,
    docker_shared_dir as docker_shared_dir,
)
from ..tasks import spawn


def _rebuild_managed_system_prompt(
    *,
    harness_name: str,
    agent_id: str,
    shared_path: Path,
    profile_path: str,
    memory_path: str,
    workspace_path: str,
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
    workspace_shared_status: str = "existing",
) -> str:
    """Dispatch wrapper: write the right system-prompt file(s) for the
    agent's harness. Codex agents get ``$CODEX_HOME/AGENTS.md``; other
    harnesses use the shared Claude/Gemini prompt-file builder.
    Returns the assembled prompt body either way. The identity fields form
    immutable addressing context next to the editable root ``profile.md``;
    prompt rebuilds never rewrite memory. ``puffo_handle`` is the authenticated
    ``puffo_core.slug`` the model should call itself on the network;
    ``agent_id`` continues to name local storage and RPC namespaces.
    """
    if harness_name == "codex":
        return rebuild_agent_codex_md(
            shared_dir=shared_path,
            profile_path=Path(profile_path),
            memory_dir=Path(memory_path),
            workspace_dir=Path(workspace_path),
            codex_user_dir=agent_codex_user_dir(agent_id),
            agent_id=agent_id,
            display_name=display_name,
            role=role,
            role_short=role_short,
            puffo_handle=puffo_handle,
            workspace_shared_status=workspace_shared_status,
        )
    return rebuild_agent_claude_md(
        shared_dir=shared_path,
        profile_path=Path(profile_path),
        memory_dir=Path(memory_path),
        workspace_dir=Path(workspace_path),
        claude_user_dir=agent_claude_user_dir(agent_id),
        gemini_user_dir=agent_home_dir(agent_id) / ".gemini",
        agent_id=agent_id,
        display_name=display_name,
        role=role,
        role_short=role_short,
        puffo_handle=puffo_handle,
        workspace_shared_status=workspace_shared_status,
    )


logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0

# Consecutive WS reconnect failures before runtime.json health flips to
# "server_unreachable". With the WS client's 1s→30s doubling backoff the
# fifth failure lands ~15s into an outage (~40s when each attempt burns a
# connect timeout): a blip surviving one or two reconnects never flips,
# while an operator checking `agent list` sees the truth within the
# minute instead of "ok" for hours. One successful reconnect clears it.
_WS_DEGRADE_THRESHOLD = 5


def _claude_cli_api_key(daemon_cfg: DaemonConfig, harness_name: str) -> str:
    if harness_name != "claude-code":
        return ""
    return claude_cli_api_key(daemon_cfg)


def build_docker_runtime(
    daemon_cfg: DaemonConfig, agent_cfg: AgentConfig,
):
    """Construct the Docker runtime owner for a ratified Driver.

    The returned preparer owns only container placement and host assets;
    Claude Code and Codex protocol behavior stays in the normal Drivers.
    """
    from ..agent.harness.runtime.docker_runtime import DockerRuntimePreparer

    return DockerRuntimePreparer(daemon_cfg, agent_cfg)


def _puffo_cli_keystore_dir() -> Path:
    """Where ``puffo-cli`` saves identities. Mirrors the Rust
    ``directories`` crate (``ProjectDirs::from("ai", "puffo",
    "puffo-cli").data_dir().join("keys")``).

    Windows: ``%APPDATA%\\puffo\\puffo-cli\\data\\keys``
    macOS:   ``~/Library/Application Support/ai.puffo.puffo-cli/keys``
    Linux:   ``$XDG_DATA_HOME/puffo/puffo-cli/keys``
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "puffo" / "puffo-cli" / "data" / "keys"
        return (
            Path.home()
            / "AppData"
            / "Roaming"
            / "puffo"
            / "puffo-cli"
            / "data"
            / "keys"
        )
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "ai.puffo.puffo-cli"
            / "keys"
        )
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "puffo" / "puffo-cli" / "keys"


def _ensure_agent_identity_imported(agent_id: str, slug: str) -> None:
    """Copy ``<slug>.json`` from puffo-cli's keystore into the
    per-agent puffo-agent keystore if missing. Without this the
    worker crash-loops on ``identity not found``. No-op when the
    destination exists or the source is missing."""
    if not slug:
        return
    dest = agent_dir(agent_id) / "keys" / f"{slug}.json"
    if dest.exists():
        return
    src = _puffo_cli_keystore_dir() / f"{slug}.json"
    if not src.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logger.info(
            "agent %s: imported identity from puffo-cli keystore (%s → %s)",
            agent_id,
            src,
            dest,
        )
    except OSError as exc:
        logger.warning(
            "agent %s: failed to import identity from %s: %s",
            agent_id,
            src,
            exc,
        )


def _build_puffo_core_client(
    agent_cfg: AgentConfig,
    agent_id: str,
    daemon_cfg: DaemonConfig | None = None,
):
    """Construct a PuffoCoreMessageClient from the agent's config.
    ``daemon_cfg`` carries the host-wide tunables (currently the
    long-message redaction thresholds); accepts ``None`` so legacy
    test seeds that didn't have a daemon config keep working with
    the dataclass defaults.
    """
    from ..agent.message_store import MessageStore
    from ..agent.puffo_core_client import PuffoCoreMessageClient, max_image_edge_px
    from ..crypto.http_client import PuffoCoreHttpClient
    from ..crypto.keystore import KeyStore

    pc = agent_cfg.puffo_core
    bridge = None
    if pc.transport == "bridge":
        # T23 keyless transport: no identity file exists to import;
        # the bridge authenticates with the sandbox token instead.
        # Keystore/http below are still constructed (both lazy — they
        # never touch disk/network in __init__); phase 2 replaces
        # their call sites.
        from ..agent.bridge_client import CloudBridgeClient

        bridge = CloudBridgeClient(pc.server_url, pc.sandbox_token, pc.slug)
    else:
        _ensure_agent_identity_imported(agent_id, pc.slug)
    ks_dir = str(agent_dir(agent_id) / "keys")
    ks = KeyStore(ks_dir)
    # Bridge agents dispatch outbound tool work keyless over the unsigned
    # ``/v2/cloud-agents/*`` routes; ``route.py`` reuses ``client.http``, so
    # the in-process ws-local cfg's ``keyless`` accessor reads True here.
    http = PuffoCoreHttpClient(
        pc.server_url,
        ks,
        pc.slug,
        keyless=(pc.transport == "bridge"),
    )
    ms = MessageStore(str(agent_dir(agent_id) / "messages.db"))

    max_inline = (
        daemon_cfg.max_inline_message_chars
        if daemon_cfg is not None
        else MAX_INLINE_MESSAGE_CHARS
    )
    segment_chars = (
        daemon_cfg.segment_chars if daemon_cfg is not None else MESSAGE_SEGMENT_CHARS
    )
    catchup_stale_hours = (
        daemon_cfg.catchup_stale_hours
        if daemon_cfg is not None
        else DEFAULT_CATCHUP_STALE_HOURS
    )

    # The inbound-image downscale cap follows the harness's effective model
    # (Opus 4.7+ resolves 2576px, else 1568px).
    runtime_kind = agent_cfg.runtime.kind or "cli-local"
    effective_provider = resolve_effective_provider(
        runtime_kind, agent_cfg.runtime.provider
    )
    effective_harness = resolve_effective_harness(
        runtime_kind, effective_provider, agent_cfg.runtime.harness
    )
    is_codex = effective_harness == "codex"
    if is_codex:
        model = agent_cfg.runtime.model or (
            daemon_cfg.openai.model if daemon_cfg else ""
        )
    else:
        model = agent_cfg.runtime.model or (
            daemon_cfg.anthropic.model if daemon_cfg else ""
        )

    return PuffoCoreMessageClient(
        slug=pc.slug,
        device_id=pc.device_id,
        space_id=pc.space_id,
        operator_slug=pc.operator_slug,
        auto_accept_space_invitations=pc.auto_accept_space_invitations,
        auto_accept_dm=getattr(pc, "auto_accept_dm", False),
        keystore=ks,
        http_client=http,
        message_store=ms,
        workspace=str(agent_cfg.resolve_workspace_dir()),
        max_inline_chars=max_inline,
        segment_chars=segment_chars,
        agent_created_at=agent_cfg.created_at,
        image_edge_px=max_image_edge_px(model),
        catchup_stale_hours=catchup_stale_hours,
        agent_id=agent_id,
        bridge_client=bridge,
    )


class Worker:
    """Runs a single AI agent inside the daemon event loop."""

    @staticmethod
    def _clear_auth_failed_if_recoverable(
        runtime: RuntimeState,
        agent_id: str,
        log: logging.Logger,
    ) -> None:
        # Fired on refresh-success, before the next provider turn. Optimistic:
        # if the next request still 401s, the adapter auth path re-sets it.
        if runtime.health != "auth_failed":
            return
        runtime.health = "ok"
        runtime.error = ""
        runtime.save(agent_id)
        log.info(
            "agent %s: credential-refresh-success; "
            "runtime.health cleared from auth_failed back to ok",
            agent_id,
        )

    def _maybe_wake_refresher_if_auth_failed(self, agent_id: str) -> None:
        """A new batch while auth_failed: wake the refresher to re-check
        for an operator re-login now instead of waiting for the poll."""
        if self.runtime.health != "auth_failed":
            return
        if self._notify_refresh_needed is None:
            return
        try:
            self._notify_refresh_needed()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent %s: notify_refresh_needed raised: %s",
                agent_id,
                exc,
            )

    async def _run_post_warm_gate(self, agent_id: str) -> None:
        """Probe the adapter's round-trip readiness after warm() succeeds,
        reassert ``auth_failed`` if the provider's still unreachable, THEN
        release ``_warm_done``. Order matters — releasing first would let a
        queued message dispatch against an unprobed runtime."""
        try:
            probe_ok = await self._adapter.health_probe()
        except Exception as exc:
            logger.warning(
                "agent %s: health_probe raised; treating as probe-fail: %s",
                agent_id,
                exc,
            )
            probe_ok = False
        if not probe_ok:
            Worker._reassert_auth_failed_after_failed_probe(
                self.runtime,
                agent_id,
                logger,
                api_key_mode=getattr(self, "_claude_api_key_mode", False),
            )
        self.runtime.status = "running"
        self.runtime.save(agent_id)
        self._warm_done.set()

    @staticmethod
    def _reassert_auth_failed_after_failed_probe(
        runtime: RuntimeState,
        agent_id: str,
        log: logging.Logger,
        *,
        api_key_mode: bool = False,
    ) -> None:
        """``on_refresh_success`` eagerly clears ``auth_failed`` before
        the respawn, so a still-broken provider warms up looking healthy.
        When the post-warm probe fails, re-assert the failed state so the
        next refresh cycle retries. No-op unless the runtime is in the
        eager-cleared ``ok`` state."""
        if runtime.health != "ok":
            return
        runtime.health = "auth_failed"
        runtime.error = Worker._auth_failed_error(api_key_mode)
        runtime.save(agent_id)
        log.warning(
            "agent %s: post-warm health probe failed; reasserted "
            "runtime.health = auth_failed",
            agent_id,
        )

    def _build_wired_client(self):
        """Construct the core client with the worker's observers attached.

        Every runtime path must obtain its client here: one built straight
        from ``_build_puffo_core_client`` carries no transport-state
        listener, so WS reconnect streaks reach nobody and runtime.json
        keeps saying "ok" while the agent is offline — the 8/30 incident's
        health symptom.
        """
        agent_id = self.agent_cfg.id
        client = _build_puffo_core_client(
            self.agent_cfg, agent_id, daemon_cfg=self.daemon_cfg
        )
        client.transport_state_listener = self._transport_state_listener(agent_id)
        return client

    def _transport_state_listener(self, agent_id: str):
        """Feed WS reconnect streaks into runtime.json health.

        Before this, a dead transport kept reporting health="ok" with a
        fresh local heartbeat for hours (the 8/30 App Nap incident):
        "process alive" was standing in for "server reachable". Only the
        ok state is overwritten — auth_failed and the other specific
        signals stay authoritative — and only the transport-set state is
        cleared on recovery.
        """

        def note(healthy: bool, streak: int) -> None:
            rt = self.runtime
            if healthy:
                if rt.health == "server_unreachable":
                    rt.health = "ok"
                    rt.error = ""
                    rt.save(agent_id)
                    logger.info(
                        "agent %s: transport recovered; runtime.health "
                        "server_unreachable → ok",
                        agent_id,
                    )
                return
            if streak < _WS_DEGRADE_THRESHOLD or rt.health != "ok":
                return
            rt.health = "server_unreachable"
            rt.error = (
                f"server unreachable: {streak} consecutive WS reconnect "
                "failures"
            )
            rt.save(agent_id)
            logger.warning(
                "agent %s: %s; runtime.health ok → server_unreachable",
                agent_id,
                rt.error,
            )

        return note

    def _enter_auth_failed(self, agent_id: str) -> None:
        """Flip ``auth_failed`` + fire recovery (refresher kick + operator
        DM). Used on a confirmed adapter auth error so we skip the
        pointless kick-retries and go straight to recover-via-relogin."""
        rt = self.runtime
        was_ok = rt.health != "auth_failed"
        rt.health = "auth_failed"
        rt.error = Worker._auth_failed_error(
            getattr(self, "_claude_api_key_mode", False)
        )
        rt.save(agent_id)
        if self._notify_refresh_needed is not None:
            try:
                self._notify_refresh_needed()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s: notify_refresh_needed raised: %s",
                    agent_id,
                    exc,
                )
        if was_ok:
            self._on_auth_failed_enter()

    def _on_auth_failed_enter(self) -> None:
        """Fire the operator DM once per auth_failed episode. The flag
        re-arms on auth_failed CLEAR (daemon ``on_refresh_success``) and
        on a failed send, so a later genuine failure re-notifies."""
        if self._auth_failed_notification_sent:
            return
        if getattr(self, "_claude_api_key_mode", False):
            self._api_key_auth_recovery_pending = True
        self._auth_failed_notification_sent = True
        try:
            spawn(
                self._notify_operator_of_auth_failed_oauth(),
                name="notify_operator_of_auth_failed_oauth",
            )
        except Exception as exc:  # noqa: BLE001
            # Re-arm so a schedule failure retries on the next ENTER.
            self._auth_failed_notification_sent = False
            logger.warning(
                "agent %s: couldn't schedule auth-failed DM: %s",
                self.agent_cfg.id,
                exc,
            )

    async def _notify_operator_of_auth_failed_oauth(self) -> None:
        """DM the operator the bilingual OAuth-expired recovery copy.
        Re-arms the dedup flag on a transient failure (client not warm,
        or send raised) so the next ENTER retries instead of staying
        silently gated."""
        client = self._client
        if client is None:
            # Still warming — re-arm so a later ENTER retries.
            self._auth_failed_notification_sent = False
            logger.warning(
                "agent %s: auth-failed DM skipped — client not yet warm",
                self.agent_cfg.id,
            )
            return
        operator_slug = getattr(client, "operator_slug", "") or ""
        if not operator_slug:
            # No operator to DM; the red-dot UI is the only signal. Stay
            # gated — re-arming would respin on every 401.
            logger.warning(
                "agent %s: auth-failed but no operator_slug — not DMing",
                self.agent_cfg.id,
            )
            return
        from ..agent._invite_strings import (
            format_anthropic_api_key_rejected,
            format_codex_oauth_expired,
            format_oauth_expired,
        )

        display_name = getattr(self.agent_cfg, "display_name", "") or self.agent_cfg.id
        # Codex agents need the Codex recovery command, not the Claude
        # one; otherwise the operator runs the wrong CLI and assumes
        # the alert is broken. Harness is the cheapest signal we have.
        runtime = getattr(self.agent_cfg, "runtime", None)
        harness = getattr(runtime, "harness", "") if runtime is not None else ""
        if getattr(self, "_claude_api_key_mode", False):
            text = format_anthropic_api_key_rejected(
                self.agent_cfg.id, display_name
            )
        elif harness == "codex":
            text = format_codex_oauth_expired(self.agent_cfg.id, display_name)
        else:
            text = format_oauth_expired(self.agent_cfg.id, display_name)
        try:
            await client._send_dm(operator_slug, text, root_id="")
        except Exception as exc:
            # Transient send failure — re-arm for the next ENTER.
            self._auth_failed_notification_sent = False
            logger.exception(
                "agent %s: auth-failed DM to %s raised: %s",
                self.agent_cfg.id,
                operator_slug,
                exc,
            )
            return
        logger.info(
            "agent %s: notified operator @%s of auth failure",
            self.agent_cfg.id,
            operator_slug,
        )

    @staticmethod
    def _auth_failed_error(api_key_mode: bool) -> str:
        if api_key_mode:
            return (
                "Claude Code API key was rejected. Update anthropic.api_key "
                "in daemon.yml, keep anthropic.cli_use_api_key enabled, "
                "restart puffo-agent, then send this agent a message."
            )
        return provider_failure_message("authentication")

    def _enter_drained(self, agent_id: str, resets_at: int | None = None) -> None:
        """``drained`` + one operator DM per episode. No refresher kick."""
        from ..agent._usage_markers import DRAINED_RUNTIME_ERROR

        rt = self.runtime
        was_ok = rt.health != "drained"
        rt.health = "drained"
        rt.error = DRAINED_RUNTIME_ERROR
        rt.save(agent_id)
        if resets_at is not None:
            self._drained_resets_at = resets_at
        if was_ok:
            self._on_drained_enter()

    def _on_drained_enter(self) -> None:
        """One DM per episode; re-arms on successful turn / failed send."""
        if self._drained_notification_sent:
            return
        self._drained_notification_sent = True
        try:
            spawn(self._notify_operator_of_drained(), name="notify_operator_of_drained")
        except Exception as exc:  # noqa: BLE001
            self._drained_notification_sent = False  # re-arm
            logger.warning(
                "agent %s: couldn't schedule drained DM: %s",
                self.agent_cfg.id,
                exc,
            )

    async def _notify_operator_of_drained(self) -> None:
        """Bilingual quota DM; re-arm semantics of the auth-failed sibling."""
        client = self._client
        if client is None:
            self._drained_notification_sent = False  # still warming: re-arm
            logger.warning(
                "agent %s: drained DM skipped — client not yet warm",
                self.agent_cfg.id,
            )
            return
        operator_slug = getattr(client, "operator_slug", "") or ""
        if not operator_slug:
            # stay gated: re-arming would respin every drained turn
            logger.warning(
                "agent %s: drained but no operator_slug — not DMing",
                self.agent_cfg.id,
            )
            return
        from ..agent._invite_strings import format_codex_drained, format_drained

        display_name = getattr(self.agent_cfg, "display_name", "") or self.agent_cfg.id
        runtime = getattr(self.agent_cfg, "runtime", None)
        harness = getattr(runtime, "harness", "") if runtime is not None else ""
        resets_at = getattr(self, "_drained_resets_at", None)
        if resets_at is None:
            # error bodies rarely carry a time — predict from /usage, best-effort
            from .control.usage_snapshot import predicted_reset_epoch

            try:
                resets_at = await predicted_reset_epoch(harness or "claude-code")
            except Exception:  # noqa: BLE001
                resets_at = None
            if resets_at is not None:
                self._drained_resets_at = resets_at
        formatter = format_codex_drained if harness == "codex" else format_drained
        text = formatter(self.agent_cfg.id, display_name, resets_at=resets_at)
        try:
            await client._send_dm(operator_slug, text, root_id="")
        except Exception as exc:
            self._drained_notification_sent = False  # re-arm
            logger.exception(
                "agent %s: drained DM to %s raised: %s",
                self.agent_cfg.id,
                operator_slug,
                exc,
            )
            return
        logger.info(
            "agent %s: notified operator @%s of quota exhaustion",
            self.agent_cfg.id,
            operator_slug,
        )

    @staticmethod
    def _clear_drained(
        runtime: RuntimeState,
        agent_id: str,
        log: logging.Logger,
    ) -> None:
        """``drained`` → ``ok``; leaves every other health alone."""
        if runtime.health != "drained":
            return
        runtime.health = "ok"
        runtime.error = ""
        runtime.save(agent_id)
        log.info("agent %s: runtime.health drained → ok", agent_id)

    @staticmethod
    def _flip_health_in_progress(
        runtime: RuntimeState,
        agent_id: str,
        log: logging.Logger,
    ) -> None:
        """Override any sticky red with ``in_progress`` at batch-top."""
        if runtime.health == "in_progress":
            return
        runtime.health = "in_progress"
        runtime.error = ""
        runtime.save(agent_id)
        log.info("agent %s: runtime.health → in_progress", agent_id)

    @staticmethod
    def _resolve_health_on_success(
        runtime: RuntimeState,
        agent_id: str,
        log: logging.Logger,
        *,
        recover_auth_failed: bool = False,
    ) -> None:
        """Transition ``in_progress`` → ``ok``; skip any in-turn red."""
        if runtime.health != "in_progress" and not (
            recover_auth_failed and runtime.health == "auth_failed"
        ):
            return
        previous_health = runtime.health
        runtime.health = "ok"
        runtime.error = ""
        runtime.save(agent_id)
        log.info(
            "agent %s: runtime.health %s → ok", agent_id, previous_health
        )

    def _resolve_health_after_success(self, agent_id: str) -> None:
        recovering_api_key = (
            getattr(self, "_claude_api_key_mode", False)
            and getattr(self, "_api_key_auth_recovery_pending", False)
        )
        self._resolve_health_on_success(
            self.runtime,
            agent_id,
            logger,
            recover_auth_failed=recovering_api_key,
        )
        if recovering_api_key:
            self._api_key_auth_recovery_pending = False
            self._auth_failed_notification_sent = False
        # a completed turn is proof quota is available again
        Worker._clear_drained(self.runtime, agent_id, logger)
        self._drained_notification_sent = False
        self._drained_resets_at = None

    @staticmethod
    def _fallback_unhandled_error_if_stuck_in_progress(
        runtime: RuntimeState,
        agent_id: str,
        turn_error: str | None,
        log: logging.Logger,
    ) -> None:
        """``in_progress`` → ``unhandled_error`` when a non-retry-able
        exception left the flip stuck. Distinct from ``unknown`` so the
        CLI / heartbeat can surface it.
        """
        if runtime.health != "in_progress":
            return
        runtime.health = "unhandled_error"
        runtime.error = turn_error or "turn raised; no category red set"
        runtime.save(agent_id)
        log.warning(
            "agent %s: runtime.health → unhandled_error (%s)",
            agent_id,
            runtime.error,
        )

    @staticmethod
    def _mark_api_error_abandoned_if_in_progress(
        runtime: RuntimeState,
        agent_id: str,
        turn_error: str | None,
        log: logging.Logger,
    ) -> None:
        """Record a bounded provider retry budget reaching its terminal edge."""
        if runtime.health != "in_progress":
            return
        runtime.health = "api_error_abandoned"
        runtime.error = turn_error or "provider retry budget exhausted"
        runtime.save(agent_id)
        log.warning(
            "agent %s: runtime.health → api_error_abandoned (%s)",
            agent_id,
            runtime.error,
        )

    @staticmethod
    def _mark_provider_failure_if_in_progress(
        runtime: RuntimeState,
        agent_id: str,
        turn_error: str | None,
        log: logging.Logger,
    ) -> None:
        """Record a categorized provider failure without calling it unhandled."""
        if runtime.health != "in_progress":
            return
        runtime.health = "provider_error"
        runtime.error = turn_error or "provider could not complete the turn"
        runtime.save(agent_id)
        log.warning(
            "agent %s: runtime.health → provider_error (%s)",
            agent_id,
            runtime.error,
        )

    def __init__(
        self,
        daemon_cfg: DaemonConfig,
        agent_cfg: AgentConfig,
        *,
        notify_refresh_needed: Optional[Callable[[], None]] = None,
        ensure_fresh_token: Optional[Callable[[], Awaitable[bool]]] = None,
        ws_local_hub=None,
    ):
        self.daemon_cfg = daemon_cfg
        self.agent_cfg = agent_cfg
        # Set for ws-local agents; the Worker idles and registers an
        # attach point instead of running a harness consumer.
        self._ws_local_hub = ws_local_hub
        # CredentialRefresher hook: a 401 in a reply short-circuits the 2-min poll.
        self._notify_refresh_needed = notify_refresh_needed
        # Blocking pre-delivery refresh via the daemon mutex; True iff the token
        # has time left. None for runtimes without claude OAuth (api-puffo, ws-local).
        self._ensure_fresh_token = ensure_fresh_token
        # In-memory dedup for the auth_failed ENTER operator DM;
        # re-armed on credential refresh-success (daemon
        # on_refresh_success) and on a failed send.
        self._auth_failed_notification_sent = False
        self._drained_notification_sent = False
        self._drained_resets_at: int | None = None
        self._claude_api_key_mode = (
            agent_cfg.runtime.kind in {RUNTIME_CLI_LOCAL, RUNTIME_CLI_DOCKER}
            and bool(
                _claude_cli_api_key(
                    daemon_cfg,
                    agent_cfg.runtime.harness or "claude-code",
                )
            )
        )
        self._api_key_auth_recovery_pending = False
        self.runtime = RuntimeState(
            status="starting",
            started_at=int(time.time()),
            msg_count=0,
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._restart_required = False
        self._adapter: Adapter | None = None
        # Held here so ``stop()`` can close the SQLite + WS handles
        # the client owns. Required on Windows so ``messages.db*``
        # files release before ``puffo-agent agent archive`` renames.
        self._client = None
        # Signalled when warm() finishes (success, failure, or skipped).
        # Daemon awaits this to serialise heavy startup across workers.
        self._warm_done = asyncio.Event()
        # Proactive (between-turns) profile reload state. The refresh
        # watcher (see ``_run``) and the turn-start ``_process_refresh_flags``
        # call share this lock so a flag is consumed exactly once —
        # whichever path wins takes it; the loser hits the early-return
        # when the flags are already gone. ``_turn_active`` gates the
        # watcher so it never applies mid-generation (deferred to the
        # turn-start path). ``_refresh_now`` is a signal-driven wake so
        # SIGHUP can beat the 250 ms idle poll.
        self._reload_lock = asyncio.Lock()
        self._turn_active = False
        self._refresh_now = asyncio.Event()

    def notify_refresh(self) -> None:
        """Wake the proactive refresh watcher now (sub-poll latency).
        Called from the daemon's SIGHUP handler, which runs on the loop
        thread. No-op safe: setting an already-set Event is idempotent,
        and if the worker has no watcher yet (pre-``_run``) the flag is
        simply observed on the first watcher tick."""
        self._refresh_now.set()

    async def _proactive_refresh_tick(self, flag_paths, apply) -> bool:
        """One proactive-watcher iteration's decision + apply. Skips when
        a turn owns flag consumption (``_turn_active``) or no flag in
        ``flag_paths`` is pending; otherwise takes ``_reload_lock`` (the
        same lock the turn-start path holds → consume-once), re-checks
        ``_turn_active``, and awaits ``apply`` (the bound
        ``_process_refresh_flags`` call). Returns whether ``apply`` ran.
        Exceptions from ``apply`` are swallowed so the watcher never
        kills the worker — the turn-start path is the backstop."""
        if self._turn_active:
            # A turn owns flag consumption; never apply mid-turn.
            return False
        if not any(p.exists() for p in flag_paths):
            # Cheap exists() check before taking the lock.
            return False
        async with self._reload_lock:
            if self._turn_active:
                # A turn started between the check and the lock; defer to
                # the turn-start path.
                return False
            try:
                await apply()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s: proactive refresh failed: %s",
                    self.agent_cfg.id,
                    exc,
                )
            return True

    async def _refresh_watcher_loop(
        self,
        flag_paths,
        apply,
        *,
        interval: float = 0.25,
    ) -> None:
        """Proactive between-turns profile reload loop. Polls
        ``flag_paths`` every ``interval`` s (waking immediately when
        ``notify_refresh()`` fires) so a ``profile.md`` / host-sync /
        session edit takes effect while the agent is IDLE — instead of
        only lazily at the next turn. Each pending flag is funneled into
        ``apply``, the bound turn-start ``_process_refresh_flags`` /
        ``adapter.reload`` primitive (adapter-only reload: the bridge WS
        on ``self._client`` and the worker are untouched). No-op unless a
        flag is present, so native/desktop runtimes see no behavioral
        change beyond applying an already-written flag sooner. Runs as a
        sibling task of ``heartbeat``; ends when ``_stop`` is set."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._refresh_now.wait(),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                pass
            self._refresh_now.clear()
            if self._stop.is_set():
                break
            await self._proactive_refresh_tick(flag_paths, apply)

    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self.runtime.status = "starting"
        self.runtime.error = ""
        self.runtime.save(self.agent_cfg.id)
        self._task = spawn(self._run(), name="run")
        return self._task

    @property
    def restart_required(self) -> bool:
        """Whether a post-start fatal failure requires daemon replacement."""
        return (
            self._restart_required
            and self._task is not None
            and self._task.done()
        )

    async def wait_warm(self, timeout: float | None = None) -> bool:
        """Block until warm() finishes or the worker exits early.
        Returns True only when startup reached ``running``."""
        try:
            await asyncio.wait_for(self._warm_done.wait(), timeout=timeout)
            return self.runtime.status == "running"
        except asyncio.TimeoutError:
            return False

    def set_profile_cache(
        self,
        slug: str,
        display_name: str,
        avatar_url: str,
    ) -> None:
        """Cross-process bridge for the MCP ``get_user_info`` tool —
        the subprocess fetches fresh from puffo-server then POSTs the
        result here via the data service, so the next daemon render
        sees the fresh display_name + avatar without waiting for the
        TTL. No-op until the worker's PuffoCoreMessageClient has been
        constructed (warm() hasn't completed yet)."""
        if self._client is not None:
            self._client.set_profile(slug, display_name, avatar_url)

    def host_mcp_context(self):
        """Build a ``HostMcpContext`` from this worker's live state.
        Returns None until ``warm()`` has built the message client."""
        client = self._client
        if client is None:
            return None
        from .host_mcp_handler import HostMcpContext
        from .state import agent_home_dir

        runtime = self.agent_cfg.runtime
        runtime_kind = getattr(runtime, "kind", "") or "cli-local"
        provider = resolve_effective_provider(
            runtime_kind, getattr(runtime, "provider", "")
        )
        harness = resolve_effective_harness(
            runtime_kind,
            provider,
            getattr(runtime, "harness", ""),
        )
        return HostMcpContext(
            agent_id=self.agent_cfg.id,
            slug=client.slug,
            operator_slug=client.operator_slug,
            host_home=Path.home(),
            agent_home=agent_home_dir(self.agent_cfg.id),
            harness=harness,
            keystore=client.keystore,
            http_client=client.http,
            message_client=client,
            send_coordinator=getattr(client, "send_delegate", None),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                # Bounded wait so a wedged WS recv / subprocess pipe
                # can't hang shutdown. Cancellation usually completes
                # in milliseconds.
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent %s: worker task didn't exit within 10s of cancel — "
                    "moving on to adapter cleanup anyway",
                    self.agent_cfg.id,
                )
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter is not None:
            try:
                # Bounded wait so a wedged ``docker stop`` can't
                # deadlock shutdown. 30s covers docker's 10s SIGTERM
                # grace plus our own ``docker stop -t 5`` and drains.
                await asyncio.wait_for(self._adapter.aclose(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent %s: adapter aclose timed out after 30s — "
                    "container may still be running, run `docker ps` to check",
                    self.agent_cfg.id,
                )
            except Exception as exc:
                logger.warning(
                    "agent %s: adapter aclose failed: %s",
                    self.agent_cfg.id,
                    exc,
                )
        await self._close_client()
        self.runtime.status = "stopped"
        self.runtime.save(self.agent_cfg.id)

    async def _close_client(self) -> None:
        """Release a partially or fully started message client."""
        if self._client is None:
            return
        # Release WS + SQLite handles. Required on Windows so
        # ``messages.db*`` is renamable by ``agent archive``.
        try:
            await asyncio.wait_for(self._client.stop(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s: client.stop timed out after 10s",
                self.agent_cfg.id,
            )
        except Exception as exc:
            logger.warning(
                "agent %s: client.stop failed: %s",
                self.agent_cfg.id,
                exc,
            )
        self._client = None

    def _runtime_info(self) -> dict[str, object]:
        rt = self.agent_cfg.runtime
        kind = getattr(rt, "kind", "") or "cli-local"
        if kind == RUNTIME_WS_LOCAL:
            return {
                "kind": kind,
                "provider": "",
                "harness": "",
                "model": "",
                "inference_level": "",
            }
        provider = getattr(rt, "provider", "")
        configured_harness = getattr(rt, "harness", "")
        if (
            not provider
            and kind in (RUNTIME_CLI_LOCAL, RUNTIME_CLI_DOCKER)
            and configured_harness
        ):
            supported = HARNESS_PROVIDERS.get(configured_harness, frozenset())
            if len(supported) == 1:
                provider = next(iter(supported))
        provider = resolve_effective_provider(kind, provider)
        harness = resolve_effective_harness(kind, provider, configured_harness)
        daemon_cfg = getattr(self, "daemon_cfg", None)
        provider_cfg = getattr(daemon_cfg, provider, None)
        model = getattr(rt, "model", "") or getattr(provider_cfg, "model", "")
        info: dict[str, object] = {
            "kind": kind,
            "provider": provider,
            "harness": harness,
            "model": model,
            "inference_level": getattr(rt, "inference_level", ""),
        }
        adapter = getattr(self, "_adapter", None)
        context_limits = getattr(adapter, "context_limits", None)
        limits = context_limits() if callable(context_limits) else (None, None)
        env_overrides = getattr(self.agent_cfg, "env_overrides", {})
        if harness == "claude-code":
            from .control.context_telemetry import build_context_runtime

            info.update(
                build_context_runtime(
                    model=getattr(adapter, "model", "") or model,
                    max_context=limits[0],
                    auto_compact_threshold=limits[1],
                    env_overrides=env_overrides,
                )
            )
        elif harness == "codex":
            from .control.context_telemetry import (
                compact_threshold_pct,
                configured_compact_pct,
            )

            configured_pct = configured_compact_pct(
                "codex", env_overrides
            )
            info.update({
                "max_context": limits[0],
                "auto_compact_threshold_pct": (
                    configured_pct
                    if configured_pct is not None
                    else compact_threshold_pct(limits[0], limits[1])
                ),
            })
        if harness in {"claude-code", "codex"}:
            max_context = int(info.get("max_context") or 0)
            threshold_pct = info.get("auto_compact_threshold_pct")
            runtime_state = getattr(self, "runtime", None)
            if (
                runtime_state is not None
                and (
                    runtime_state.max_context != max_context
                    or runtime_state.auto_compact_threshold_pct != threshold_pct
                )
            ):
                runtime_state.max_context = max_context
                runtime_state.auto_compact_threshold_pct = threshold_pct
                runtime_state.save(self.agent_cfg.id)
        return info

    def _build_status_reporter(self, client) -> StatusReporter:
        from ..agent.processing_receipts import ProcessingReportDispatcher

        bridge = getattr(client, "_bridge", None)
        processing_reports = None
        if bridge is None and not bool(getattr(client.http, "keyless", False)):
            store = getattr(client, "store", None)
            if store is not None:
                processing_reports = ProcessingReportDispatcher(store, client.http)
                client._processing_reports = processing_reports
                register_connected = getattr(client, "add_connected_callback", None)
                if callable(register_connected):
                    register_connected(processing_reports.on_transport_connected)
        reporter = StatusReporter(
            client.http,
            runtime_health_provider=lambda: self.runtime.health,
            runtime_provider=self._runtime_info,
            status_sender=bridge.send_status if bridge is not None else None,
            processing_reports=processing_reports,
        )
        if bridge is not None:
            bridge.add_connected_callback(reporter.report_current_status)
        return reporter

    async def _run_ws_local(self) -> None:
        """ws-local agents run no harness consumer. Build the client,
        register an attach point, and idle — the bridge's /v1/ws-local
        route brings the agent online when a tool connects."""
        agent_id = self.agent_cfg.id
        try:
            if not self.agent_cfg.puffo_core.is_configured():
                raise RuntimeError(
                    f"agent {agent_id!r}: puffo_core block in agent.yml is incomplete"
                )
            client = self._build_wired_client()
            self._client = client
            reporter = self._build_status_reporter(client)
            identity = client.keystore.load_identity(client.slug)
            root_public_key = Ed25519KeyPair.from_secret_bytes(
                decode_secret(identity.root_secret_key)
            ).public_key_bytes()
            point = AttachPoint(
                slug=self.agent_cfg.puffo_core.slug,
                agent_id=agent_id,
                agent_cfg=self.agent_cfg,
                client=client,
                reporter=reporter,
                ack_timeout_s=180.0,
                ping_interval_s=30.0,
                root_public_key=root_public_key,
            )
        except Exception as e:
            logger.error(
                "agent %s: ws-local init failed: %s", agent_id, e, exc_info=True
            )
            self.runtime.status = "error"
            self.runtime.error = str(e)
            self.runtime.save(agent_id)
            self._warm_done.set()
            return

        self.runtime.status = "running"
        self.runtime.save(agent_id)
        self._warm_done.set()
        if self._ws_local_hub is not None:
            self._ws_local_hub.register(point)

        # Push display_name / avatar_url / role / soul to the server identity.
        # The regular runtimes do this post-warm; ws-local idle skips that path,
        # so without this a freshly-created ws-local agent shows blank in the UI.
        async def _ws_local_profile_sync() -> None:
            from .profile_sync import sync_full_profile

            try:
                await sync_full_profile(self.agent_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent %s: ws-local profile sync failed: %s", agent_id, exc
                )

        spawn(_ws_local_profile_sync(), name="ws_local_profile_sync")
        logger.info("agent %s: ws-local idle, awaiting tool attach", agent_id)
        try:
            await self._stop.wait()
        finally:
            if self._ws_local_hub is not None:
                self._ws_local_hub.unregister(point)
            self.runtime.status = "stopped"
            self.runtime.save(agent_id)

    async def _run(self) -> None:
        if (self.agent_cfg.runtime.kind or "") == RUNTIME_WS_LOCAL:
            await self._run_ws_local()
            return
        from .worker_run import StandardWorkerRun

        await StandardWorkerRun(self).run()


async def _process_refresh_flags(
    *,
    agent_id: str,
    harness_name: str,
    shared_path: Path,
    profile_path: str,
    memory_path: str,
    workspace_path: str,
    puffo,
    adapter,
    refresh_agent_flag: Path,
    refresh_host_sync_flag: Path,
    refresh_session_flag: Path,
    refresh_provider_auth_flag: Path,
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
    workspace_shared_status: str = "existing",
) -> None:
    """Consume worker refresh flags in one idle-boundary adapter reload.

    Resource and credential changes preserve session identity. Only an explicit
    session refresh starts a new logical and native provider conversation.
    """
    host_sync_seen = refresh_host_sync_flag.exists()
    agent_seen = refresh_agent_flag.exists()
    session_seen = refresh_session_flag.exists()
    provider_auth_seen = refresh_provider_auth_flag.exists()
    if not (host_sync_seen or agent_seen or session_seen or provider_auth_seen):
        return

    if host_sync_seen:
        _sync_refresh_host_assets(agent_id)

    new_prompt: str | None = None
    if agent_seen:
        try:
            new_prompt = _rebuild_managed_system_prompt(
                harness_name=harness_name,
                agent_id=agent_id,
                shared_path=shared_path,
                profile_path=profile_path,
                memory_path=memory_path,
                workspace_path=workspace_path,
                display_name=display_name,
                role=role,
                role_short=role_short,
                puffo_handle=puffo_handle,
                workspace_shared_status=workspace_shared_status,
            )
            prompt_changed = new_prompt != puffo.system_prompt
            puffo.system_prompt = new_prompt
            logger.info(
                "agent %s: system prompt rebuilt from disk (changed=%s)",
                agent_id,
                prompt_changed,
            )
        except Exception as exc:
            logger.warning(
                "agent %s: refresh_agent failed: %s",
                agent_id,
                exc,
            )

    try:
        await adapter.reload(
            new_prompt if new_prompt is not None else puffo.system_prompt,
            with_session=session_seen,
        )
        if provider_auth_seen:
            logger.info(
                "agent %s: provider runtime reloaded after credential replacement",
                agent_id,
            )
    except Exception as exc:
        logger.warning(
            "agent %s: adapter.reload after refresh failed: %s",
            agent_id,
            exc,
        )

    for flag in (
        refresh_host_sync_flag,
        refresh_agent_flag,
        refresh_session_flag,
        refresh_provider_auth_flag,
    ):
        try:
            flag.unlink()
        except OSError:
            pass


def _sync_refresh_host_assets(agent_id: str) -> None:
    try:
        from .state import agent_home_dir, sync_host_mcp_servers, sync_host_skills

        host_home = Path.home()
        agent_home = agent_home_dir(agent_id)
        skill_count = sync_host_skills(host_home, agent_home)
        merged_mcp, _unreachable = sync_host_mcp_servers(host_home, agent_home)
        logger.info(
            "agent %s: refresh_host_sync (skills=%d mcp=%d)",
            agent_id,
            skill_count,
            merged_mcp,
        )
    except Exception as exc:
        logger.warning(
            "agent %s: refresh_host_sync failed: %s",
            agent_id,
            exc,
        )


_CLAUDE_DIR_SUBDIRS = ("agents", "commands", "skills", "hooks")


def _seed_claude_dir(claude_dir: Path) -> None:
    """Create the Claude Code project-level skeleton (agents/,
    commands/, skills/, hooks/). Idempotent — never overwrites."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    for sub in _CLAUDE_DIR_SUBDIRS:
        (claude_dir / sub).mkdir(exist_ok=True)
