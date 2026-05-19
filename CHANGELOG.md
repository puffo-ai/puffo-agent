# Changelog

All notable changes to `puffo-agent` are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.9.0b1] — 2026-05-19

_Pre-release published to TestPyPI only — not for general install._

Beta-promote of `0.9.0a3` with the post-review polish folded in.
Code is unchanged from the architectural pass; this is the first
version intended for macOS-colleague verification.

### Polished (from PR #20 review)

- **`portal/credential_refresh.py`**: dropped the unconditional
  top-level import of `KEYCHAIN_POLL_INTERVAL_SECONDS` from the
  macos package. Now lazy-imported inside `_external_rotation_loop`,
  matching every other macos touchpoint in the file. The
  platform-agnostic module no longer pulls the macos package into
  its import graph on Linux/Windows.

- **`portal/credential_refresh.py`**: restored the `cwd=host_home`
  justification comment in `FileBackend.refresh` that was
  accidentally dropped in the 0.9.0a3 backend-abstraction port.
  The choice is non-obvious — `claude --print` writes project-
  transcript files into cwd, so pointing it at the daemon's launch
  directory would leak them. Mirror of the rationale that landed in
  PUF-221 PR #32 round 4.

- **`agent/adapters/local_cli.py`**: adapter spawn no longer
  re-installs the PATH shim on every worker spawn. `KeychainBackend.
  bootstrap()` already runs `install_path_shim` once at daemon
  start; the adapter now just computes `shim_dir(home_dir())` for
  the env var. Removes per-spawn `write_text` + `chmod` overhead and
  closes a concurrent-spawn write-write race the bootstrap path
  doesn't have.

- **`portal/diagnostic.py`**: noted in code that the `#37512` repro
  probe (the only place we deliberately set `CLAUDE_CODE_OAUTH_TOKEN
  =<real token>`) makes the token briefly visible to `ps auxe` for
  the duration of the claude subprocess. Intentional — the probe's
  whole purpose is to reproduce that issue — but worth surfacing for
  anyone running on a shared host.

- **`portal/diagnostic.py` + `macos/keychain.py`**: paired
  ``keep in sync`` warnings on `_run_sandboxed_claude_oneshot`
  (sync, diagnostic-side) and `refresh_via_oneshot` (async,
  production-side). They share env shape + claude args; the
  diagnostic loses its load-bearing value the moment they drift.

- **`portal/diagnostic.py`** module docstring: stale
  `puffo_agent.macos.credential_manager` → `puffo_agent.macos.keychain`
  (one-word swap, the post-rebase module path).

Tests unchanged: **726 passed / 7 skipped / 0 failed** locally on
Windows.

## [0.9.0a3] — 2026-05-19

_Pre-release published to TestPyPI only — not for general install._

### Changed

- **macOS Keychain integration on top of the daemon-owned
  `CredentialRefresher`.** Claude Code 2.x stores its OAuth token in
  the system Keychain (`"Claude Code-credentials"`), and per-agent
  `$HOME` overrides don't isolate Keychain access (the ACL is keyed
  on UID + signing identity, not HOME). Without daemon-level
  intermediation, the host's `claude` binary running under a
  puffo-agent worker re-prompts the ACL every spawn and the per-agent
  `.credentials.json` files diverge from the operator's main CLI
  view. GitHub issue anthropics/claude-code#37512 compounds the
  problem: setting `CLAUDE_CODE_OAUTH_TOKEN` triggers a
  `security delete-generic-password "Claude Code-credentials"`
  cleanup on exit that kicks the user's main CLI / VS Code extension
  off Keychain entirely.

  This release extends the 0.8.8 `CredentialRefresher` with a
  pluggable backend abstraction so the macOS Keychain path and the
  Linux/Windows host-file path share the same daemon-owned lock,
  agent fan-out, and 401-wake invariants while differing only on
  storage:

  - `CredentialBackend` Protocol (`portal/credential_refresh.py`) —
    four methods: `expires_in_seconds`, `refresh` (async, returns
    `RefreshOutcome.{REFRESHED, UNCHANGED, FAILED}`), `sync_to_agent`,
    and `bootstrap`.
  - `FileBackend` — preserves bit-identical 0.8.8 behavior on
    Linux/Windows. Host `~/.claude/.credentials.json` is canonical;
    refresh spawns `claude --print` with `HOME=host_home`; sync is a
    symlink (or copy fallback) via `link_host_credentials`. External
    rotation propagates atomically through the symlink — no
    external-poll needed.
  - `KeychainBackend` — macOS path. Keychain is canonical; the
    daemon maintains a cache at
    `~/.puffo-agent/run/claude-credentials.json` (atomic-write JSON
    blob, chmod 600); refresh runs a sandboxed `claude --print`
    under a tempdir `HOME` seeded from the cache so claude rotates
    the token against Anthropic and writes the new blob back to the
    sandbox file (which we then copy to the cache); writeback to
    Keychain is best-effort so the operator's main CLI sees the new
    token; `sync_to_agent` is a per-agent file copy (Keychain ACL
    forces this — symlinking the cache wouldn't help because the
    per-agent `claude` process still goes through Keychain anyway).
  - `CredentialRefresher` itself stays platform-agnostic: owns the
    `asyncio.Lock` (single-writer across all refreshes), the agent
    home registry, the `_refresh_request` event for 401-wake, the
    2-minute file-expiry poll, and the post-tick fan-out that calls
    `backend.sync_to_agent(agent_home)` for every registered agent.
    Daemon `daemon.py` picks the backend at startup based on
    `is_macos()`.
  - **External-rotation poll** (macOS-only): `KeychainBackend`
    exposes `poll_external_rotation()` and the refresher runs it as
    a sibling task on `KEYCHAIN_POLL_INTERVAL_SECONDS = 5 * 60`. The
    poll re-reads Keychain (silent after the first "Always Allow"
    grant), diffs against the last propagated blob, and on detected
    change updates the cache + triggers fan-out via the same
    `_sync_views` path. This catches rotations done by the
    operator's main `claude` CLI or by an agent's own claude
    subprocess self-refreshing on a 401 — neither of which the
    daemon initiates, so neither would be visible to siblings
    without this poll.
  - **PATH shim** (issue #37512 workaround): every daemon start
    writes a bash shim to
    `~/.puffo-agent/run/keychain-shim/security` that intercepts
    `security delete-generic-password "Claude Code-credentials"`
    and silently no-ops it, passing every other `security`
    invocation through to `/usr/bin/security`. The shim dir is
    prepended to `$PATH` for every per-agent `claude` spawn and for
    the daemon's own refresh oneshot.
  - **`local_cli` adapter macOS env-injection**: on macOS, each
    agent's `claude` subprocess gets `CLAUDE_CONFIG_DIR=<agent_home>/.claude`
    and `PATH=<shim_dir>:<original PATH>`. **Deliberately does NOT
    set `CLAUDE_CODE_OAUTH_TOKEN`** — that env var triggers the
    same bug #37512 cleanup path and would defeat the shim's
    protection. The per-agent `.credentials.json` is materialised by
    `KeychainBackend.sync_to_agent` whenever the refresher's tick
    fans out, so claude reads from there normally. Linux/Windows
    spawn env is unchanged.
  - Diagnostic CLI: `puffo-agent test ...` subcommand tree with 5
    probes (`keychain-read`, `keychain-write`, `refresh-flush`,
    `keychain-survives-token-env`, `full-probe`) plus a
    side-effectful `refresh-flush-forced` (gated on `--yes`). Writes
    a redacted-markdown probe report to
    `~/.puffo-agent/probe-report.md`. Tokens are shown only as
    `len=NNN sha256_prefix=XXXXXXXX`. Each probe SKIPs cleanly on
    non-Darwin so the same CLI works as a sanity-check tool on
    Linux/Windows.

  The PUF-221 public API (`CredentialRefresher.register_agent` /
  `unregister_agent` / `notify_refresh_needed` / `run_loop` /
  `expires_in_seconds`) is unchanged — the refactor is invisible to
  the daemon's reconcile loop and to `Worker`'s `notify_refresh_needed`
  callback. The 0.8.8 `host_home=...` constructor signature still
  works (it implicitly constructs a `FileBackend`) so the existing
  `tests/test_credential_refresher.py` pins the public-API contract
  without modification.

### Tests

- `tests/test_macos_credential_manager.py` (~30 tests) — pure-function
  tests for `CredentialCache`, `install_path_shim`, the keychain
  read/write primitives (subprocess.run mocked), `refresh_via_oneshot`
  + `_run_claude_oneshot` (asyncio.create_subprocess_exec mocked),
  `bootstrap_from_keychain`, plus end-to-end tests for
  `KeychainBackend` plugged into `CredentialRefresher`
  (`expires_in_seconds` cache-vs-Keychain path, `refresh` returning
  `REFRESHED`/`UNCHANGED`/`FAILED`, `sync_to_agent` writing per-agent
  files, `poll_external_rotation` detecting changes / swallowing
  read failures, the refresher's fan-out invoking `sync_to_agent`
  on every registered agent, FD-leak regression for the timeout
  drain path).
- `tests/test_macos_diagnostic.py` (~19 tests) — report rendering,
  token redaction (raw tokens never appear in stdout / saved
  report), off-macOS SKIPPED path on every probe, on-macOS happy
  path with mocked subprocess, forced-expiry helpers, and the
  `refresh-flush-forced` `--yes` gate.
- `tests/test_credential_refresher.py` (the 12 0.8.8 pinned tests)
  pass unchanged against the refactored class — the backend
  abstraction is invisible to the public API.

Full suite: **726 passed / 7 skipped / 0 failed** on Windows
(macOS-specific assertions still execute because they monkeypatch
`is_macos` to True; symlink-unavailable skips on Windows are normal).

## [0.8.8] — 2026-05-19

### Changed

- **Claude OAuth credential refresh is now daemon-owned — one writer,
  N readers, one shared file.** Anthropic's OAuth uses single-use
  refresh tokens: every successful `/oauth/token` rotate issues a new
  `refresh_token` and invalidates the previous one. With the
  pre-existing per-agent `Adapter.refresh_ping` design, N long-lived
  `claude` subprocesses each held an in-memory RT and refreshed
  independently; the first one to rotate invalidated the disk RT
  every other agent was about to read, and the daemon's per-agent
  `claude --print "ok"` writer races burned each other's in-memory
  copies. The net was a silent 401 cascade with no operator-visible
  signal until every agent flipped `auth_failed` at once.

  New `CredentialRefresher` (`portal/credential_refresh.py`) lives in
  the daemon process. Its `run_loop` polls
  `~/.claude/.credentials.json` every 2 minutes, triggers a refresh
  via `claude --print "ok"` with `HOME=<host_home>` when
  `expiresAt - now < 10 min`, **or** when `notify_refresh_needed()`
  is called. The refresh subprocess is serialized by an
  `asyncio.Lock` so Anthropic's RT rotation can never be observed
  mid-write by another caller. Three triggers feed the refresher:

  1. **Host expiry** — 2-minute poll + 10-minute safety margin.
  2. **Per-agent expiry** — collapses into (1) because per-agent
     credentials are symlinks (or, on Windows without Developer
     Mode, copies that `_sync_views` re-writes every tick) of the
     same host file.
  3. **401 from a worker turn** — `_handle_suppressed_reply` fires
     an `on_auth_failure` callback wired through `Worker.__init__`
     into `refresher.notify_refresh_needed()`, short-circuiting the
     2-minute poll so the refresh kicks within ~1 s. The callback
     fires **only on the auth-class leak branch** (not on rate-
     limit / 5xx / quota leaks — credential rotation isn't the fix
     for an Anthropic outage). Callback exceptions are swallowed so
     a broken hook can't break the suppression flow, and
     `runtime.health = "auth_failed"` happens **before** the
     callback so a guaranteed-throw callback can't leave health in
     a torn state.

  After every tick — whether the tick refreshed, skipped, or errored
  — the refresher fans out `state.link_host_credentials(host_home,
  agent_home)` to each registered agent. This means an operator
  running `claude /login` externally on the device propagates to
  every agent's view on the next 2-minute poll without daemon
  restart (empirically observed 2026-05-19 05:34–05:37 UTC: operator
  `claude /login` rewrote the host file and both running agents
  recovered automatically on their next inbound message).

  CLI surface: per-agent `agent refresh-ping <id>` is replaced by
  one `agent refresh-token` subcommand that writes a sentinel file
  the daemon's reconcile loop forwards into the in-process
  `asyncio.Event`. Same `stop_request` pattern that already exists
  in `state.py`, symmetric for operator-side debugging.

  Retires PUF-207 (startup `_check_startup_auth_or_pause` probe +
  `auth_healthy` flag), PUF-213 (`_next_refresh_tick` adaptive
  cadence in worker), PUF-217 (`HOME=agent_home` rewrite-symlink
  bug), and PUF-218 (per-agent `_REFRESH_LOCK`). Net ~656 LOC
  removed: `Adapter.refresh_ping` / `_run_refresh_oneshot` (both
  adapters) / `_credentials_expires_in_seconds` / `auth_healthy` /
  `_REFRESH_LOCK` / `_check_startup_auth_or_pause` /
  `_next_refresh_tick`, plus 4 whole test files (`test_refresh_ping
  .py`, `test_credential_refresh_policy.py`,
  `test_refresh_oneshot_home_env.py`, `test_worker_startup_auth.py`)
  and 2 surgical removals from `test_cli_session_recovery.py`.

### Tests

- `tests/test_credential_refresher.py` — 12 new tests: disk-read
  variants (fresh / missing / corrupt), `register_agent` /
  `unregister_agent` set semantics, `notify_refresh_needed` event
  bit, `_tick` no-refresh-when-fresh path with explicit
  `link_host_credentials` assertion (locks the "sync regardless of
  refresh result" contract), `_tick` refresh-when-close-to-expiry
  asserting `env["HOME"] == host_home`, `_tick` refresh on
  `triggered_by_agent=True`, view-sync fan-out across multiple
  registered agents, `run_loop` stop-event observance, `run_loop`
  wake-on-event with <1 s latency assertion (vs. the 100 s poll
  interval), CLI sentinel-file write/read/clear round trip.
- `tests/test_worker_error_suppress.py` — 3 new tests on the
  `on_auth_failure` callback contract: positive (auth-class leak
  fires callback + flips health), negative (429 leak suppresses
  reply but does NOT fire callback or flip health), defensive
  (raising callback → health still flips, suppression still
  returns True).

Full suite: **676 passed / 1 skipped / 0 failed** post-cleanup (was
706 pre-cleanup; –30 from the 4 deleted test files and 2 surgical
removals).

## [0.8.7] — 2026-05-19

### Added

- **Agent now reacts to space/channel membership-exit events on the
  WS push.** Before this release the WS event router (`_handle_event`
  in `puffo_core_client.py`) only handled `invite_to_space` /
  `invite_to_channel` / the synthetic auto-accept
  `accept_channel_invite`. Every other event kind was silently
  dropped, so when the agent was removed from a space or kicked from
  a channel its caches and outbound queues stayed stale until the
  next reconnect / poll, and the operator got no notification about
  what changed.

  Pairs with `puffo-server` review/events (PR #74) which now
  broadcasts these events to the affected slug too via the
  `extra_ws_targets` union (otherwise the agent would never see
  them — by the time the post-loop fan-out runs, the engine has
  already removed it from the member set).

  New handlers:

  - `leave_space` (signer = self) — fires on both a self-signed
    `LeaveSpace` and the puffo-server #74 synthetic cascade emitted
    when an operator leaves (`signature ==
    "server-auto:agent-cascade-leave-space"`). Evicts
    `_channel_space` / `_channel_name_cache` / `_space_name_cache`
    for the space; DMs the operator with reason-specific wording.

  - `remove_from_space` (target = self) — same cache eviction; DM
    names the kicker so the operator knows who removed their agent.

  - `leave_channel` (signer = self) — voluntary channel exit; cache
    eviction only, no DM (operator-initiated, they already know).

  - `remove_from_channel` (target = self) — per-channel eviction; DM
    references both the channel and its parent space.

  - `cancel_space_invite` / `cancel_channel_invite` — if the agent
    DM'd the operator a `y`/`n` prompt for the now-withdrawn invite,
    send a follow-up in the same thread so the operator doesn't
    reply `y` to nothing (server would return InviteNotFound 400).
    No-op when no prompt was outstanding (auto-accepted, never
    DM'd).

### Hardened

- **Synthetic cascade `LeaveSpace` events are re-verified against
  `/spaces` before any visible side effect.** The synthetic events
  puffo-server emits for agent-operator cascades carry a server-set
  marker signature (`"server-auto:agent-cascade-leave-space"`) —
  not a real ed25519 signature — so they aren't
  cryptographically authenticatable on the wire. Trusting them
  blindly meant a buggy server, WS redelivery on reconnect, or a
  malicious server could trick the agent into evicting caches and
  DMing the operator about a membership change that never
  happened.

  Fix: before applying the visible side effects of a synthetic
  cascade event, re-confirm with `GET /spaces` (authoritative
  membership API). The check returns `True` (still listed —
  contradicts cascade, bail), `False` (confirmed gone — proceed),
  or `None` (network error — fall through to permissive cleanup so
  a transient flake doesn't strand the agent in a space it's been
  cascaded out of).

  Scoped to the one event kind where this defense matters; real
  signed events skip the recheck (the server-side engine already
  verified the signature before broadcasting, and re-verifying on
  the agent side would need a full ed25519-verify + cert-chain
  resolution stack for little real benefit given the server is
  authoritative for membership state anyway).

### Changed

- **MCP tool surface for channel discovery is now three explicit
  tools instead of one implicitly-cross-space `list_channels`.**

  - `list_spaces()` — every space the agent is a member of.
    `GET /spaces` is server-filtered, so the result reflects
    authoritative permissions: anything in the list is a space
    the agent can write to.
  - `list_channels_in_space(space_id)` — channels in one named
    space. `space_id` is required (empty → MCP tool error) so
    the LLM can't accidentally fall back on the legacy
    `cfg.space_id` for routing.
  - `list_channels_in_all_spaces()` — convenience: walks
    `GET /spaces` plus one `GET /spaces/<sp>/channels` per
    space, grouped output. Same behaviour as the old
    `list_channels` cross-space rewrite, renamed for clarity.

  The old `list_channels` walked `cfg.space_id`'s event stream
  and returned `(no space configured)` when that field was
  empty, both of which made it impossible for an agent
  operating in multiple spaces to enumerate its true channel
  surface. Three-tool split gives the LLM precise vocabulary
  for "which spaces" vs "which channels in space X" and removes
  the last MCP-surface dependency on `cfg.space_id`. Primer
  (`shared_content.py`), tool allowlist (`mcp/config.py`), and
  log-message tool-name examples (`local_cli.py` /
  `docker_cli.py`) are all updated to point at the new names.

### Hardened

- **`send_fallback_message` no longer silently routes to
  `self.space_id` on cache miss.** The daemon's reply-when-LLM-
  skipped-`send_message` path used to fall back to the legacy
  home space when the in-memory channel→space map didn't have
  the inbound channel — under cross-space deployments or after
  a kick/cascade that evicted the cache, that sent the reply
  to the wrong space (server 403) or worse to a space the
  agent had just been removed from. Now drops the reply with a
  clear log line; legacy `self.space_id` slot stays in the
  constructor but no code path consults it for routing.

- **Membership-exit eviction now drops the persistent
  `channel_space_map` rows too, not just the in-memory cache.**
  The MCP subprocess reads `channel_space_map` via
  `lookup_channel_space` to resolve `send_message` targets;
  the 0.8.7 in-memory `_evict_*_caches` left those rows in
  place, so after a kick/cascade the LLM would resolve a
  channel it had been evicted from to the old space, pay a
  round-trip, and get a 403. Added
  `MessageStore.unmark_channel_space` and
  `unmark_channel_space_for_space`; wired into both eviction
  helpers (now async). Errors are logged + swallowed so a
  transient DB hiccup never blocks the visible WS-handler
  reaction.

### Fixed

- **`list_channel_members(channel)` returns the right space's
  roster across cross-space membership.** Previously hardcoded
  the URL with `cfg.space_id` regardless of where the channel
  actually lived; cross-space callers got the wrong roster, a
  404, or a 403. Now resolves the channel→space mapping from
  the event-driven cache (see "Added" below) and round-trips
  to the channel's actual parent space.

- **`send_message` / `send_message_with_attachments` resolve the
  target space from the event-driven cache, not a multi-call
  walk over `/spaces` + `/spaces/<sp>/channels`.** The pre-fix
  resolver was both slow (worst-case N+1 round-trips) and racy
  (channel might not be in the per-space listing yet when the
  AcceptChannelInvite has just been committed). Misses now
  raise a clear MCP error rather than silently falling back to
  `cfg.space_id`.

### Added

- **Membership events feed the channel→space cache so the agent
  can address a freshly-joined channel before the first inbound
  message lands on it.** `_handle_event` now records the
  `(channel_id, space_id)` pair from `invite_to_channel` (when
  the agent is the invitee), `accept_channel_invite` (when the
  agent is the signer), and `create_channel` (always — the
  server only fans `create_channel` to space members).
  `_accept_invite` also writes the mapping synchronously after
  posting the accept so the immediately-following intro-nudge
  send doesn't race the WS echo back.

- **Agent now reacts to space/channel membership-exit events on the
  WS push.** Before this release the WS event router (`_handle_event`
  in `puffo_core_client.py`) only handled `invite_to_space` /
  `invite_to_channel` / the synthetic auto-accept
  `accept_channel_invite`. Every other event kind was silently
  dropped, so when the agent was removed from a space or kicked from
  a channel its caches and outbound queues stayed stale until the
  next reconnect / poll, and the operator got no notification about
  what changed.

  Pairs with `puffo-server` review/events (PR #74) which now
  broadcasts these events to the affected slug too via the
  `extra_ws_targets` union (otherwise the agent would never see
  them — by the time the post-loop fan-out runs, the engine has
  already removed it from the member set).

### Tests

40+ new pytest tests across:

- `tests/test_membership_events.py` — 16 tests covering each
  exit-event handler's happy path, ignore-when-not-target,
  cache eviction contents (in-memory + persistent), operator
  DM text, the synthetic-cascade re-check behavior, and the
  `operator_slug = ""` early-provisioning case.
- `tests/test_puffo_core_tools.py` — 10 tests for the new
  three-tool surface plus the existing
  `_handle_event` cache-admission tests (admission per event
  kind + signer gate, `send_message` / `list_channel_members`
  cache-miss raises).
- `tests/test_channel_intro_nudge.py` — cache-admission tests
  for the synthetic auto-accept path and the `create_channel`
  / `invite_to_channel` mapping recording.
- `tests/test_worker_integration.py` — pin
  `send_fallback_message` drop-on-unknown-channel behavior.

## [0.8.6] — 2026-05-18

### Fixed

- **Python-version precheck moved to `puffo_agent/__init__.py` and
  fires before any submodule of the package is parsed.** Users on
  Python 3.9 / 3.10 used to see a deep `SyntaxError` / `ImportError`
  from inside the submodule chain at `cli.py`'s import block — the
  actual cause (wrong Python version) was buried under the
  wrong-looking message, and users frequently mis-attributed it as
  "my Python is broken" (Shiva, FB-149).

  Initial fix (0.8.5-rc, PR #27) added the precheck at the top of
  `cli.py`. That covered the dominant case but had a subtle
  weakness: Python parses an entire module before executing any line,
  so if `cli.py` itself ever used Python 3.11-only syntax (`match` /
  `case`, PEP 604 unions in expression position, exception groups)
  the file would fail at parse time **before** the precheck had a
  chance to run, and the user would be back to "deep SyntaxError"
  from inside `cli.py`.

  Move it one layer up: `puffo_agent/__init__.py` runs first when
  Python resolves any `puffo_agent.*` submodule import — including
  the entry-point's `from puffo_agent.portal.cli import main`. The
  precheck now fires **before `cli.py` is even parsed**, so a future
  3.11-only edit to `cli.py` (or any other submodule) can't bypass
  the guard. `puffo_agent/__init__.py` itself stays deliberately
  parseable on Python 3.6+ (f-strings only, no PEP 604, no `match`)
  — documented in the module docstring so the constraint isn't lost.

  `pyproject.toml`'s `requires-python = ">=3.11"` remains the
  canonical metadata gate; this is the runtime safety net for users
  on venvs whose interpreter doesn't match the metadata (e.g.
  installed with `--ignore-requires-python` or older pip).

  Tests in `test_cli_python_version.py` updated to import
  `_require_python_311` from `puffo_agent` instead of
  `puffo_agent.portal.cli`. Same 4-case matrix (rejects 3.9.18 /
  3.10.12, passes 3.11.0 / 3.14.4); each reject branch asserts exit
  code 1 + version-in-message + presence of at least one of
  `pyenv` / `brew` / `python.org` upgrade hints. (PUF-206)

- **PUF-217 test fixture: `os.rename` → `os.replace` + symlink-skip
  guard for Windows.** The PUF-217 test fixture's fake-claude
  subprocess used `os.rename(tmp_target, target)` to mimic Claude
  CLI's atomic tmp+rename. On POSIX `os.rename` atomically replaces
  an existing target, but on Windows it raises `FileExistsError`;
  the cross-platform atomic-replace primitive is `os.replace`. Two
  `os.rename` call sites in `test_refresh_oneshot_home_env.py`
  swapped to `os.replace`. Additionally, the two tests that assert
  on `agent_creds.is_symlink()` now gate on
  `_symlinks_available(tmp_path)` and `pytest.skip` when the host
  can't create symlinks — mirrors the existing pattern in
  `test_host_credentials.py` (Windows without Developer Mode falls
  through `link_host_credentials`'s copy-mode branch, so the
  symlink-distribution invariant these tests assert on doesn't
  apply there). Linux CI continues to exercise the full assertions;
  Windows now skips cleanly instead of failing. Full suite:
  **642 passed / 9 skipped / 0 failed**.

- **`credential_refresh` worker loop now scales its sleep to the
  token's TTL instead of a fixed 10-minute tick.** Pre-fix, the
  worker slept a flat `CREDENTIAL_REFRESH_TICK_SECONDS = 10 * 60`
  between probes. When a Claude CLI / Anthropic OAuth access token's
  remaining lifetime was short enough that its refresh window —
  `CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS = 5 * 60` from
  `base.Adapter` — fell entirely inside one tick interval, the loop
  could skip the refresh entirely. Token expired, next turn 401'd,
  the FB-88 silent-can't-recover surface. The classic case: probe
  at T sees TTL=600s → "above 300s threshold, skip"; next tick at
  T+600 sees TTL=0 — refresh window was T+300 → T+600, fully
  swallowed.

  New `_next_refresh_tick(expires_in)` helper in `worker.py`
  computes the next sleep adaptively:
  - `None` TTL (sdk / chat-only adapters without a credentials
    file) → fall back to `default_tick = 10 min` — the loop is a
    pure health heartbeat in this mode.
  - TTL far in the future → `default_tick` (capped — above the
    refresh window the loop is just a slow heartbeat).
  - TTL just above the window → wake `threshold` seconds before
    expiry so the next tick lands inside the refresh window with
    margin. Concretely: TTL=600s, threshold=300s → next tick =
    600 - 300 = 300s, so the helper wakes at TTL=300s right at
    the threshold — refresh fires.
  - TTL inside the window OR negative (already expired) → clamp
    to `CREDENTIAL_REFRESH_TICK_FLOOR_SECONDS = 60` so a
    sustained refresh failure doesn't dogpile `_REFRESH_LOCK`,
    but we still tick fast enough to retry promptly.

  The helper is a pure function with module-constant defaults —
  production call site is just `_next_refresh_tick(expires_in)`;
  tests override `default_tick` / `threshold` / `floor` to pin
  synthetic values that decouple them from production-constant
  drift. The `try / except Exception → None` wrap around
  `self._adapter._credentials_expires_in_seconds()` at the call
  site gracefully degrades to `default_tick` if an adapter hook
  ever throws.

  8 new tests in `test_credential_refresh_policy.py`: None /
  far-future / just-above-window (returns pre-window margin) /
  at-window (clamps to floor) / below-window (clamps to floor) /
  already-expired (clamps to floor) / default-cap on long horizon
  / parametric custom-thresholds. Adapter-level integration
  (entire `credential_refresh` loop end-to-end against a real
  clock) deferred to a 5+ day soak as the PR description notes.

  Defense-in-depth triad with PUF-207 (startup OAuth probe +
  auto-pause) and PUF-214 (worker-error suppression at the egress
  boundary). PUF-213 prevents the refresh window from being
  silently missed during runtime; PUF-207 catches at startup;
  PUF-214 keeps any error string that slips past either out of
  user-visible channels. (PUF-213)

### Added

- **Agent startup auto-pauses when the OAuth probe says "auth is
  dead."** When a cli-local agent's persisted session re-spawned
  with an expired or revoked OAuth token, `Worker._run()` used to
  proceed straight into `warm()` and the first turn — at which
  point Claude CLI emitted `Not logged in. Please run /login` and
  that string leaked into the channel as if it were the agent's
  reply (FB-159 / Sheri / Yasushi mis-attributed as "my Python is
  broken"). The startup path had no signal at all that auth was
  broken before the first message hit production.

  Now `Worker._run()` fires `adapter.refresh_ping()` between init
  and `warm()`. The new `_check_startup_auth_or_pause()` helper
  reads `adapter.auth_healthy` afterwards:
  - `None` (sdk / chat-only adapters — no credential file) or
    `True` (probe succeeded) → proceed unchanged.
  - `False` (probe reported auth failure) → mutate the runtime to
    `status=paused, health=auth_failed`, populate `runtime.error`
    with a four-step recovery prompt (open a separate Terminal,
    not from within an agent's own shell, run `claude /login`,
    then `puffo-agent agent resume <id>`; mentions the venv full-
    path caveat), persist, and bail before `warm()` ever spawns
    the doomed claude subprocess. `self._warm_done.set()` is
    still called on the early-return path so any caller awaiting
    `wait_for_warm()` doesn't hang.

  Not sticky: the existing periodic refresh tick at
  `worker.py:737-748` can flip `runtime.health` back to `ok` if
  the operator re-authenticates and the next probe succeeds. The
  operator still needs to manually `puffo-agent agent resume <id>`
  (auto-resume would risk a tight pause/resume loop if auth
  flaps); the message in `runtime.error` is the recovery script.

  Defense-in-depth triad with PUF-213 (adaptive refresh tick
  prevents staleness during runtime) and PUF-214 (worker-layer
  error-string leak suppression if either misses) — together they
  close the silent-can't-recover surface for the FB-159 / FB-105
  class.

  Tests in `test_worker_startup_auth.py` (7 cases): the helper
  matrix (`auth_healthy=False` pauses with the full recovery
  prompt incl. agent id ≥2 occurrences; `None` / `True` proceed;
  persistence to disk; not-sticky on probe-success), plus two
  call-site tests using a `_startup_call_site` helper that
  mirrors the exact pause-or-warm-then-set production block from
  `Worker._run()` — asserts `warm_done.is_set()` on BOTH the
  pause early-return path AND the proceed-then-warm path, catching
  any future refactor that drops the `self._warm_done.set()` line
  before the early return. Same call-site-mirroring pattern as
  PUF-214's `_fallback_call_site` helper. (PUF-207)

## [0.8.5] — 2026-05-18

### Fixed

- **`mcp__puffo__send_message` (and `send_message_with_attachments`)
  now auto-correct a non-root `root_id`.** When an agent passed the
  envelope id of a *reply* (rather than the thread's true root) as
  `root_id`, the message used to encrypt with that reply id as
  `thread_root_id` and land in a sub-thread that human clients don't
  surface — no failure signal returned to the agent. We hit this
  twice live on 2026-05-18 (clone-report `msg_38364760` and
  build-test-report `msg_9e8f1a83`, both threaded under root
  `msg_610fec10`), and both messages vanished from the operator's
  view.

  New helper `_resolve_root_id(root_id, data_client)` runs alongside
  `_coerce_root_visibility`: it looks the supplied envelope up in the
  local message store and substitutes its own `thread_root_id`
  before encrypting, then appends a correction note to the tool
  response so the agent learns the rule from the result on the spot
  rather than depending on the primer being current.

  Failure-mode contract: lookup miss / transport error / `DataNotFound`
  fall through with the original id plus a soft warning — the send
  still completes (better to land in the wrong thread than drop the
  message). Cycle in the chain or chain deeper than 4 levels is
  treated as corrupt data: the helper preserves the original `root_id`
  and surfaces a loud "could not resolve to a true root" warning
  instead of auto-correcting to a value it can't trust.

  Walk is capped at 4 levels with cycle detection — on healthy data
  the walk terminates in one hop (per `message_store.py`'s schema,
  `thread_root_id` always points at a true root); the multi-hop walk
  is corruption defense for relay data shapes that shouldn't exist.

  Tests in `test_puffo_core_tools.py`: 8 unit tests on `_resolve_root_id`
  (empty/whitespace, true root unchanged, single-level + depth-2 walk,
  lookup miss, transport error, real `DataNotFound`, cycle + depth-cap
  preservation), plus 5 integration tests on `send_message` /
  `send_message_with_attachments` — including the two real
  2026-05-18 live-failure envelope IDs as parametrised cases for a
  date-stamped regression anchor. Full suite: **596 passed / 1
  skipped / 0 failed**. (PUF-200)

- **Worker-layer error-string leaks no longer reach the channel.**
  Pre-fix, when Claude CLI's OAuth died mid-session (FB-159 Sheri /
  Yasushi class) or the Anthropic API returned an authentication /
  rate-limit / quota error (FB-105 class), the worker fed the raw
  error string straight into `client.send_fallback_message(...)` —
  operators saw their personal/family DMs polluted with
  `Not logged in. Please run /login` and
  `[puffo-agent system message] session errored on rate limiting…`,
  and the agent retried each new message every few seconds because
  no upstream layer recognised the state.

  Two-part fix at the worker egress boundary:

  *Pattern-based suppression* — anchored regexes match only the
  worker-emitted error signatures (legitimate agent prose mentioning
  "rate limit" / "login" / "authentication" passes through unchanged).
  Sources: [Claude Code error reference](https://code.claude.com/docs/en/errors)
  message-to-recovery table + [Claude API errors](https://platform.claude.com/docs/en/api/errors)
  canonical `<type>_error` identifiers. 12 patterns ship: usage-limit
  variants (`You've hit your <session|weekly|Opus> limit`,
  `Credit balance is too low`), CLI-wrapped server errors
  (`API Error: Request rejected (429)`,
  `API Error: Server is temporarily limiting requests`,
  `API Error: Repeated 529 Overloaded errors`,
  `API Error: 500 ... Internal server error`), OAuth recovery class
  (`OAuth token revoked|has expired`, `Invalid API key`,
  `This organization has been disabled`), and the safe subset of API
  identifiers (`authentication_error`, `rate_limit_error`,
  `overloaded_error`, `billing_error`, `permission_error`,
  `timeout_error`, plus the kick-text echo signature). Identifiers
  with high false-positive risk against legitimate prose
  (`invalid_request_error`, `not_found_error`, `api_error`, and
  generic phrases like `Prompt is too long`, `Request timed out`,
  `Unable to connect to API`) are deliberately excluded per
  doc-driven audit.

  *Randomised backoff after suppression* — `_handle_suppressed_reply`
  returns `(suppressed, backoff_seconds)`. Both `Worker._run` call
  sites (`on_message_batch` and `on_api_error_retry`) unpack the
  tuple and `await asyncio.sleep(backoff)` on suppression instead of
  immediately re-entering the loop. Backoff is `random.uniform(15.0,
  60.0)` — drops the steady-state leak frequency ~30× without
  grounding the agent (auto-pause was considered and rejected — too
  high a recall-risk for a single leak). Module-level
  `_SUPPRESSION_BACKOFF_MIN/MAX_SECONDS` constants let tests pin
  against the same values.

  Auth-class leaks (the 5 patterns in `_AUTH_ERROR_PATTERNS`,
  including the new OAuth-token-revoked / Invalid-API-key /
  disabled-org additions) also flip `runtime.health=auth_failed`
  symmetrically across both scopes, surfacing on `puffo-agent
  status` and in the bridge UI without polluting the channel.
  Non-auth leaks get a "usually self-recovers — investigate the
  daemon log if persistent" message instead of misdirecting the
  operator to `claude /login`.

  Tests in `test_worker_error_suppress.py`: 46 total, including
  parametrized positive matches for all 12 patterns + auth-class
  classification, parametrized skip-list negatives that pin the
  high-FP exclusions, backoff distribution at 100 samples (range +
  non-degenerate-random guard), and a real-`asyncio.sleep`-monkeypatch
  call-site test that exercises the production shape end-to-end.
  Full suite: **628 passed / 1 skipped / 0 failed**.

  Defense-in-depth context: pairs with PUF-207 (startup OAuth verify)
  and PUF-213 (adaptive credential refresh) — PUF-207 catches at
  startup, PUF-213 prevents staleness during runtime, this catches
  the egress leak if either misses. (PUF-214)

- **OAuth-refresh probe no longer clobbers the agent's credentials
  symlink on Linux.** On cli-local + Linux, `_run_refresh_oneshot`
  used to override `HOME` to the agent's per-agent home dir, sending
  Claude CLI's atomic `tmp+rename` write through the agent's
  symlinked `.credentials.json`. The rename **replaced the symlink
  with a regular file** at the agent path, leaving the canonical
  host file stale. The next `_credentials_expires_in_seconds` tick
  called `link_host_credentials`, whose copy-mode fast-path detected
  `agent_creds.exists() and not is_symlink()` plus an mtime mismatch
  and ran `shutil.copy2(host_creds, agent_creds)` — the **stale host
  file overwrote the fresh-token agent file**. From the daemon's
  perspective `expiresAt` never advanced, the adaptive-cadence floor
  kicked in at 60s, and the refresh cycle dogpiled indefinitely.
  Live in-band repro happened on the operator's Linux box at 18:30
  on 2026-05-18, mid-implementation.

  Fix drops the `HOME` / `USERPROFILE` override in
  `_run_refresh_oneshot` so the refresh subprocess inherits the
  daemon's env (the operator's HOME). Claude writes to
  `/home/<operator>/.claude/.credentials.json` directly; the
  per-agent symlinks distribute the fresh token via read-through.
  The "symlink survives atomic rename writes" claim in `state.py`'s
  `link_host_credentials` docstring is now retroactively correct
  because rename never targets the symlink path. Long-lived
  `ClaudeSession` agent subprocesses are unaffected —
  `_ensure_session` still sets `HOME=<agent_home>` for normal turn
  execution; only the short-lived refresh probe changes scope.

  Operational caveat (flagged for post-deploy): dropping the HOME
  override means the refresh subprocess now activates the
  *operator's* `.claude.json` MCP servers (Gmail / Drive / Calendar
  / Notion / PDF + any locally-installed) instead of the agent's.
  Expect 2–5s of MCP startup overhead per refresh.
  `REFRESH_ONESHOT_TIMEOUT_SECONDS = 120` so there's ample headroom,
  but watch for `refresh one-shot rc=0 in N.Ns` log lines staying
  under ~10s; a `--strict-mcp-config` follow-up will skip MCP
  startup in refresh-only invocations if that becomes a problem in
  practice.

  Tests in `test_refresh_oneshot_home_env.py`:
  `test_refresh_oneshot_inherits_operator_home` (env-mutation guard
  — monkeypatches `asyncio.create_subprocess_exec` and asserts
  `env["HOME"]` equals the operator's HOME, NOT the agent's
  home_dir); `test_refresh_oneshot_write_lands_at_host_path_visible_via_agent_symlink`
  (end-to-end-ish — fake claude subprocess does `tmp+rename` at the
  env's HOME path, asserts agent symlink still resolves to a file
  with the fresh `accessToken` AND
  `_credentials_expires_in_seconds()` reads back a positive TTL);
  `test_refresh_oneshot_does_not_create_regular_file_at_agent_path`
  (anti-regression — agent path remains `is_symlink()` after refresh
  + no stray `.credentials.tmp` left at the agent path; catches a
  future refactor that reintroduces a HOME override). Full suite:
  **600 passed / 1 skipped / 0 failed**.

  Defense-in-depth context: PUF-217 closes the **disk-write side**
  of the FB-88 refresh cascade. PUF-218 (deferred) will close the
  disk-read side (long-lived `ClaudeSession` reloads from disk after
  refresh). With PUF-207 (startup probe), PUF-213 (adaptive
  cadence), and PUF-214 (egress leak suppression), the OAuth
  lifecycle compound is fully closed on Linux. (PUF-217)

## [0.8.4] — 2026-05-17

### Added

- **Multi-agent `export` / `import` with device migration.** An
  operator can pack N agents on the old machine into a single
  encrypted `.puffoagent` bundle and recover them on the new machine
  with the *same* slug — outside observers see the same agent, the
  same channel/space memberships, the same profile; only the
  underlying device key rotates and the old device is auto-revoked.

  Architecture is enrollment-style, not key-copy: `device_id ↔
  device_pubkey` is 1:1, so copying keys verbatim would make the old
  and new daemon share an identity and a `/devices/{id}/revoke` call
  would lock out both. Instead, import preserves `root_secret_key`
  + `identity_cert` + `slug_binding` (the root identity), generates a
  fresh `device_signing_key` + KEM key, calls the existing
  `/devices/enroll/init` + `/devices/enroll/{nonce}/complete` to
  register the new device, then signs a `device_revocation` with the
  root key and POSTs it to `/devices/{old_device_id}/revoke`.
  Best-effort revoke: if the new device is registered but the revoke
  call 5xx's, a `pending_revoke.json` marker is written and the
  separate `revoke-pending` command can retry without re-running
  import.

  **No server-side changes.** Uses `/devices/subkeys` (which accepts
  device-key direct signing via `DeviceOrSubkeyAuth`) to register a
  temporary subkey on the old device so it can sign the enrollment
  completion, and again on the new device for the revoke call.

  **Bundle format** — `.puffoagent`: 16-byte magic
  (`PUFFO-AGENT-V1\x00\x00`) + 16-byte scrypt salt + 12-byte AES-GCM
  nonce + AES-256-GCM-encrypted inner zip. AEAD AAD covers
  `magic + salt` so header tampering fails decryption. Inner zip is
  `manifest.json` + `agents/<id>/...` per agent. Password-required at
  both ends; wrong password fails with a clean
  `"decryption failed"` rather than a vague tag error.

  **Crash safety.** Each agent imports through
  `agents/.import-staging/<id>/` and only `shutil.move`s onto
  `agents/<id>/` once enrollment is committed on the server. Daemon
  startup sweeps any leftover `.import-staging/` from a prior crash.
  Per-agent skip if the live agent dir already exists, so re-running
  `import` on the same bundle is idempotent.

  **Sanitisation.** The export drops files that are device-bound to
  the source machine and don't carry forward: `runtime.json`,
  `cli_session.json`, `messages.db`, `.puffo-agent/*.flag`,
  `.puffo-agent/current_turn.json`, `workspace/.claude/.credentials.json`.
  `messages.db` is deliberately dropped — its records are sealed
  under the *old* KEM key, which the new device can't decrypt;
  history that needs to survive a migration should be preserved
  server-side.

  **CLI.**
  - `puffo-agent agent export <id>... --dest <path>` — prompts for a
    password twice; auto-corrects `.puffoagent` extension; refuses
    overwrite without `--force`.
  - `puffo-agent agent import <src>` — prompts once; prints
    per-agent `OK / PARTIAL / SKIP / FAIL` lines plus a summary.
  - `puffo-agent agent revoke-pending [id]` — retries one agent's
    pending revoke, or sweeps all when called without an id.

  **Bridge HTTP.**
  - `POST /v1/agents/export` — JSON
    `{agent_ids, password}` → `application/octet-stream` blob.
  - `POST /v1/agents/import` — JSON
    `{bundle_b64, password}` → JSON `ImportReport`.
  - `POST /v1/agents/{id}/revoke-pending` — owner-gated retry.

  `BRIDGE_MAX_REQUEST_BYTES` raised to 64 MiB so the bridge can
  accept a base64-wrapped multi-agent bundle.

  24 new tests across `test_export_module.py`,
  `test_import_module.py` (3-phase flow against a mocked
  puffo-server, enrollment-failure cleanup, revoke-failure pending
  marker, retry happy path), and `test_bridge_export_import.py`
  (full HTTP roundtrip: export → fresh home → import →
  verify new `device_id` replaced old; wrong-password rejection;
  owner-gated revoke-pending). Full suite: 576 passed, 7 skipped.

## [0.8.3] — 2026-05-15

### Fixed

- **`send_message` no longer silently sends to the wrong space when a
  channel id isn't in the local cache.** The previous resolver fell
  back to `cfg.space_id` (the agent's home space) whenever
  `lookup_channel_space` had no record of the channel — but the
  channel may actually live in a *different* accessible space, in
  which case the next call (`/spaces/<home>/channels/<ch>/members`)
  targeted the wrong space. The relay's response in that case wasn't
  the documented 403/400; it was a 2xx with a non-JSON body, which
  the caller then `.get()`-ed and crashed three layers up with the
  opaque `'str' object has no attribute 'get'` (FB-76 root cause).

  Two-stage resolver now:
  1. local cache (`data_client.lookup_channel_space` — unchanged);
  2. on miss, walk `GET /spaces` + `GET /spaces/<sp>/channels` to
     find a definitive match across the agent's accessible spaces.

  When both miss, raise a clear unresolved-channel error rather than
  guessing. The previous `or cfg.space_id` fallback is removed
  entirely. 2 new tests in `test_puffo_core_tools.py` (discovery
  succeeds in another space; full miss → clear error). The
  `PuffoCoreHttpClient` fail-loud fix below remains as the
  safety-net for any other caller that hits the same non-JSON-2xx
  shape from the relay.

- **`PuffoCoreHttpClient` no longer hands callers a raw string body
  as if it were a parsed JSON response.** When a 2xx response carried
  a non-empty, non-JSON body — a proxy / CDN error page, a gateway
  interstitial, a plain-text error — `_do_request`'s `json.loads`
  fallback returned the raw string, and every caller of
  `get()` / `post()` (`mcp__puffo__send_message` channel resolution,
  `list_channels`, …) then did `.get()` on it and crashed three
  layers up with the opaque `'str' object has no attribute 'get'`.
  Reported via FB-76: `send_message` to a channel id and
  `list_channels` failing identically — the shared `http_client`
  layer, not an endpoint-specific bug. `_request` now raises
  `HttpError` with the actual body when a 2xx response isn't JSON, so
  the failure is diagnosable at the source. Empty 2xx bodies (204 No
  Content etc.) are unaffected. 2 new tests in `test_http_client.py`.

## [0.8.2] — 2026-05-14

### Fixed

- **A `# Soul` section whose body opens with its own heading is no
  longer read as empty — or duplicated on update.** Soul templates
  (and the operator-authored souls) open `# Soul` with a
  `# <agent-name>` title line. Both the `_profile_summary` reader and
  the `_update_profile_summary` writer detected the section's end as
  "the next heading of the same-or-higher level" — and the body's own
  opening H1 *is* such a heading:
  - **Read** closed the section instantly and returned `""`, so the
    web client's agent card showed "Soul not configured" even though
    `profile.md` carried a full soul.
  - **Write** skipped "the old body until the next heading", hit that
    same opening H1, skipped nothing, and inserted the new summary
    *above* the old body — an append/duplicate instead of a replace.
    Repeated UI soul edits stacked multiple souls into one file.

  Both paths now share one `_soul_section_span` helper: a
  same-or-higher heading only closes the section once real prose (a
  non-blank, non-heading line) has been collected. An *opening*
  heading is part of the soul; a *trailing* `# Notes` section after
  real soul prose still closes it and stays out. Follow-up to the
  0.7.6 multi-line `_profile_summary` change that introduced the
  walk-to-next-heading logic.

  4 new tests in `test_profile_summary.py` (read with an opening
  heading, write replaces an opening-heading body without
  duplication, trailing section preserved on update, append-when-
  absent). Full suite: 548 passed, 7 skipped.

## [0.8.1] — 2026-05-14

### Added

- **`puffo-agent agent reset-primer <id> ...`** — re-seed the shared
  platform primer to the installed version. The shared primer
  (`~/.puffo-agent/docker/shared/CLAUDE.md` + `skills/`) is
  seed-once: `ensure_shared_primer` never overwrites it, so a
  `puffo-agent` upgrade never reached existing installs — primer
  updates only landed on brand-new machines. The new command
  force-rewrites the managed shared files to this install's version
  (unchanged files skipped, edited ones backed up to `<file>.bak`
  first), then rebuilds each listed agent's managed `CLAUDE.md` /
  `GEMINI.md` from the fresh primer. The re-seed is global — the
  agent id list only scopes which agents get rebuilt. Running
  workers keep their loaded prompt; the rebuild takes effect on the
  worker's next restart.

### Fixed

- **`is_visible_to_human=false` on a root-level message is no longer
  a silent no-op.** Root-level (non-threaded) messages can't fold in
  the human UI — only threaded replies do — so an agent passing
  `false` on a root-level `send_message` /
  `send_message_with_attachments` was producing a message that
  rendered visible anyway but was inconsistently excluded from
  unread counts. The tools now coerce the flag back to visible (the
  message still goes out — a warning, not an error) and splice a
  note into the tool response so the agent learns at the point of
  the mistake rather than depending on a possibly-stale primer. The
  primer and tool docstrings now state that `false` only takes
  effect on threaded replies.

## [0.8.0] — 2026-05-14

### Added

- **`is_visible_to_human` — agents now mark every message as
  human-facing or agent-to-agent.** A new field on the
  signed+encrypted `MessagePayload` distinguishes messages a person
  should read from machine-to-machine chatter that human clients
  fold away. The field lives inside the E2E-encrypted payload, so
  the server stores it opaquely — no server schema change.

  - The `mcp__puffo__send_message` MCP tool takes a **required**
    `is_visible_to_human` argument — there is no default, the agent
    judges every message. Pass `true` for replies, status updates,
    and operator pings; `false` only for coordination chatter a
    human watching the channel would find pure noise.
  - `upload_file` is renamed `mcp__puffo__send_message_with_attachments`
    and gains the same required argument — it always was a real
    message-send, the name now says so.
  - Internal sends are explicit: operator-facing DMs (invite
    approvals) stay visible; the `[SILENT]`-skip fallback posts
    folded, since an agent that reached the fallback never made a
    conscious visibility call.
  - Incoming messages surface `is_visible_to_human` in the agent's
    metadata block, so receiving agents see how each message was
    classified.
  - The agent primer documents the required argument on both write
    tools and steers agents toward `send_message` over the fallback.

  Backward compatible on the wire: messages from senders that
  predate the field decrypt with `is_visible_to_human` defaulting
  to `true`.

- The `post_message` client method is renamed `send_fallback_message`
  to match its role — the `[SILENT]`-skip safety net, not a general
  post path.

### Fixed

- **An oversized inbound image can no longer dead-lock an agent.**
  Anthropic's API rejects any conversation containing an image whose
  longest edge tops 2000px ("exceeds the dimension limit for
  many-image requests — start a new session with fewer images").
  Once claude-code Read such an attachment into its session
  transcript, EVERY later turn failed wholesale and the agent was
  permanently stuck — the image analogue of the long-message
  problem fixed in 0.7.9.

  Two-part fix:
  - **Prevention** — inbound image attachments are dimension-checked
    and downscaled in place at save time (longest edge pinned to
    1568px, Anthropic's recommended max, well under the hard cap),
    so claude-code only ever loads in-bounds images. Adds a Pillow
    dependency; non-images, already-small images, and anything
    Pillow can't open are left untouched.
  - **Recovery** — if a poison reaches the API anyway (or is already
    stuck in an existing transcript), the ``claude`` session adapter
    recognises the rejection, clears the persisted session id, kills
    the subprocess, and **re-runs the same turn on a fresh session**
    (no ``--resume`` onto the poisoned transcript). The poison was
    content from an earlier turn the fresh session no longer has, so
    the re-sent message goes through — the triggering message is
    NOT dropped. Retried once; if the message itself still poisons
    the fresh session it's surfaced rather than looped.

  Existing stuck agents recover automatically on their next inbound
  message — no operator action needed.

## [0.7.9] — 2026-05-13

### Fixed

- **``mcp__puffo__list_mcp_servers`` now enumerates plugin-routed
  MCP servers too.** Operator + agent feedback: plugins installed
  via ``claude /plugin install`` (e.g. ``imessage``,
  ``chrome-devtools-mcp``) register their MCP servers under
  ``~/.claude/plugins/cache/<plugin>/<version>/.mcp.json`` — a
  third scope distinct from system (``~/.claude.json``) and agent
  (``<workspace>/.mcp.json``). The listing tool only walked the
  first two, so plugin-provided servers were invisible to the
  agent even though it could call them.

  Fix walks the plugin cache too and tags each entry ``plugin``
  with a ``(from <plugin>/<version>)`` source label so the
  operator can map server-back-to-plugin at a glance. Defensive
  on the file system: missing cache dir, missing per-version
  ``.mcp.json``, malformed JSON in one plugin's file, and
  multiple cached versions all handled without taking the whole
  listing down.

  5 new tests in ``test_agent_install.py``; the 2 pre-existing
  list tests updated to the new 3-tuple shape
  ``(scope, name, source)``.

### Added

- **Inbound long-message redaction + ``get_post_segment`` MCP
  tool.** Operators occasionally paste large code blocks into a DM
  or channel; combined with the agent's system prompt + history
  the result can blow past the provider's context window. Before
  this release that surfaced as an "API Error: Prompt is too
  long" string from claude-code, which the rate-limit retry loop
  bounced through ``--resume`` three times and then abandoned —
  but with the cursor stuck at the failed batch, any new message
  in the same thread re-triggered the same failure. From the
  operator's perspective the agent was wedged and ``restart``
  didn't help (the claude-code session still held the oversize
  transcript); ``pause`` was the only way out.

  Fix: in ``puffo_core_client.handle_envelope``, any message
  whose text exceeds ``DaemonConfig.max_inline_message_chars``
  (default 4000) gets replaced — *for the LLM view only* — with a
  ``[puffo-agent system message]`` placeholder citing the
  envelope_id, total length, segment count, sender, and a short
  preview. The full envelope is still persisted to
  ``messages.db`` unmodified. The new
  ``mcp__puffo__get_post_segment(envelope_id, segment,
  segment_size)`` tool pages the body back ``segment_chars``
  bytes at a time (default 2000) so the agent can fetch only
  what it needs.

  Logged when triggered:
  ```
  agent <id>: inlined message <env_id> truncated
  (47832 → 4000 chars, 24 segments) for prompt budget
  ```

  New tunables in ``daemon.yml``:
  ```yaml
  max_inline_message_chars: 4000   # redact above this
  segment_chars: 2000              # page size for get_post_segment
  ```

  Existing oversize content already in a claude-code session
  isn't unstuck by this release — clear the agent's
  ``.claude-session.json`` (under ``~/.puffo-agent/agents/<id>/``)
  once to force a fresh transcript on next start.

## [0.7.8] — 2026-05-13

### Added

- ``puffo-agent agent autoaccept <id> --space <space_id> --owner on|off``
  — toggles the agent's per-space ``auto_accept_owner_invite``
  flag via signed PATCH to puffo-server's new
  ``/spaces/{id}/members/me/settings`` endpoint. When ON, the
  agent silently joins any channel its space owner invites it to;
  when OFF, the invite goes through the normal DM-operator
  confirmation path. Member-invite flag is deliberately not
  exposed — the server returns 403 for agents on that field. CLI
  uses the agent's own keystore (same auth model as the
  ``profile`` subcommand: operator controls the local keystore,
  so a CLI invocation IS an operator decision).

### Changed

- **Self-introduction nudge now fires on server-side auto-accept
  too.** Previously the synthetic ``AcceptChannelInvite`` event the
  server emits when it short-circuits an InviteToChannel
  (``auto_accept_owner_invite=TRUE`` + inviter is the space owner)
  was silently ignored by the daemon's WS handler, so the agent
  never posted its 2-3-sentence intro in the auto-joined channel.
  The handler now recognises the synthetic variant via the
  ``payload.original_invite`` marker and routes through the same
  ``_enqueue_channel_intro_nudge`` path that the operator-signed
  accept already uses. Idempotent: redelivered events hit the
  existing per-channel dedup gate. 5 new unit tests pin the
  branch + negative cases (other-slug fan-out, operator-signed
  echo, malformed payload).

## [0.7.7] — 2026-05-13

### Fixed

- **Agents can now see their own sent messages in
  ``get_channel_history`` / ``get_thread_history``.** Operator-
  reported: `get_thread_history(<root>)` returned only messages
  from other senders; the agent's own replies were absent even
  though other agents in the same thread saw them fine.

  Root cause: two parallel send paths existed, and only one wrote
  locally. The daemon-internal ``PuffoCoreMessageClient.post_
  message`` (used by the fallback-reply path in worker.py) mirrored
  the outbound payload to ``messages.db`` after the server POST,
  but the MCP tool ``mcp__puffo__send_message`` (the path agents
  actually use) didn't. Both relied on the daemon's WS handler to
  not persist, because ``handle_envelope`` dropped envelopes whose
  ``sender_slug == self.slug`` at the door — to "avoid retrigger
  loops".

  The right shape (long-term) is "server-echoed-over-WS is the
  canonical proof a message was delivered, so the WS handler is
  the canonical write path for inbound + outbound alike":

  * ``handle_envelope`` no longer drops self-envelopes. The server
    fans out every recipient device in ``envelope.recipients``,
    which always includes the agent's own device (the MCP
    ``send_message`` tool puts self in the recipient list for both
    DMs and channels), so a successful send → WS echo → daemon
    persists through the same path every other message uses.
  * After the ``store.store(...)`` call, self-envelopes return
    early — they're persisted but never queued for the LLM
    (re-feeding the agent its own words would trip a turn-by-turn
    echo loop).
  * The redundant mirror-write in ``post_message`` was removed.
    Both send paths now converge on the WS-echo persistence,
    which is the single source of truth.

  Follow-up: a future change can extract ``handle_envelope`` to a
  testable method and add a unit test for the self-echo persist
  + dispatch-skip semantics. The cursor-check tests in
  ``test_thread_queue.py`` are unaffected. Full suite still 496 /
  503 (no new tests, no regressions).

- **Intro-nudge synthetic envelope is now persisted to ``messages.db``,
  and the status reporter no longer 404s on local-only envelopes.**
  Operator-reported: after joining a channel the agent received the
  ``[puffo-agent system message] You've just been added to …`` prompt
  and posted an intro, but the daemon log carried a noisy
  ``begin_turn message=intro-prompt-… failed (HTTP 404: NOT_FOUND)``
  per nudge, and the agent's own ``mcp__puffo__get_channel_history``
  call right after would return an inconsistent view (the intro it
  just saw wasn't there).

  Two changes:
  * The synthetic envelope is written to ``messages.db`` via the
    existing ``MessageStore.store`` path before being enqueued, so
    ``get_channel_history`` / ``get_message_by_envelope`` /
    ``lookup_channel_space`` all resolve it naturally. Side benefit:
    ``send_message(root_id=<intro id>)`` is now a real thread root
    locally; agents can post a reply-shape intro without producing
    a broken reference.
  * ``StatusReporter.begin_turn`` / ``end_turn`` /
    ``end_turn_batch`` recognise local-only envelope prefixes
    (currently ``intro-prompt-``) and skip the server POST. The
    run is still tracked in-memory via the returned ``run_id`` so
    the worker's batched ``end_turn_batch`` path keeps working;
    server-side rows simply never get created for envelopes the
    server never knew about. Quiets the per-nudge WARN noise
    structurally rather than via primer compliance.

  5 new tests: 1 in ``test_channel_intro_nudge.py``
  (``test_intro_nudge_persists_envelope_to_messages_db``) + 4 in
  ``test_status_reporter.py`` (``begin_turn`` skip, ``end_turn``
  skip, batch filters mixed local/real runs, all-local batch
  short-circuits). Full suite: 496 passed, 7 skipped.

- **Host Claude Code plugins now propagate to cli-local + cli-docker
  agents.** Operator-reported: plugins installed via
  ``claude /plugin install <name>@<marketplace>`` (e.g.
  ``imessage@claude-plugins-official``,
  ``chrome-devtools-mcp@claude-plugins-official``) were silently
  invisible to cli-local agents — ``mcp__puffo__list_mcp_servers``
  returned ``(no MCP servers registered)`` for the plugin-provided
  MCPs, and the per-agent ``.claude/plugins/`` directory didn't
  exist at all.

  Root cause was a missing sync step. ``seed_claude_home`` had been
  one-shot copying ``.claude/settings.json`` + ``.claude.json``, but
  nothing was bringing across:
  * ``~/.claude/plugins/`` — the marketplace clones + plugin cache
    + ``installed_plugins.json`` + ``known_marketplaces.json`` that
    contain the actual plugin code.
  * ``~/.claude/settings.json#enabledPlugins`` — the array that
    tells Claude which plugin names to load. ``seed_claude_home``
    copied this once on first start but never refreshed, so
    plugins enabled later on the host stayed invisible to the
    agent. (Plugin-provided MCP servers register through the plugin
    pipeline, not through the user-level ``mcpServers`` map that
    ``sync_host_mcp_servers`` already merges, so the existing MCP
    sync didn't cover this case.)

  Two new helpers in ``portal/state.py``, both wired into
  ``local_cli.LocalCLIAdapter._verify()`` after the existing host
  syncs:
  * ``sync_host_plugins(host_home, agent_home)`` — symlinks
    ``host_home/.claude/plugins/`` to
    ``agent_home/.claude/plugins/``. The tree is GB-scale (each
    marketplace is a git clone with history); symlink keeps the
    agent live with host installs without recopy cost. Falls back
    to ``copytree`` on Windows-without-Developer-Mode (operators
    can ``rm -rf <agent>/.claude/plugins`` to force a refresh in
    that branch). Returns the mode string for logging.
  * ``sync_host_enabled_plugins(host_home, agent_home)`` — rewrites
    just the ``enabledPlugins`` key in per-agent ``settings.json``
    from the host's. Atomic tmp+rename. Leaves other settings
    keys (theme, model, etc.) untouched. Accepts both the dict
    (``{name: true}``) and list (``[name, ...]``) shapes Claude
    Code has used historically.

  Tests in ``test_host_sync.py``: 11 new (5 plugin tree + 6
  enabledPlugins) covering symlink / copy-fallback / idempotent
  re-call / no-host-dir noop / agent-side preservation.

  cli-docker takes a different shape because the container's
  ``/home/agent/.claude`` is already an outer bind-mount: nested a
  second read-only bind-mount surfacing ``host_home/.claude/plugins``
  at ``/home/agent/.claude/plugins:ro`` (so the marketplace + cache
  + installed_plugins.json reach the in-container Claude without a
  copy), and called the same ``sync_host_enabled_plugins`` helper
  in ``_ensure_started`` so settings.json (already bind-mounted)
  carries the enabledPlugins array. The cli-docker image bakes
  node 22 + npm + python + uv, so most ``npx``/``uvx`` plugin
  commands resolve naturally; native-binary plugins (those that
  shell out to a host-only path) will still fail at use-time. 5
  new tests in ``test_docker_host_plugins.py`` covering the bind-
  mount injection, the missing-host-dir noop, the ``:ro`` flag,
  argv ordering (before image positional), and the enabledPlugins
  propagation path.

  Full suite: 491 passed, 7 skipped.

## [0.7.6] — 2026-05-12

### Added

- Bridge endpoints for the web client's 5-button agent action row:
  ``POST /v1/agents/{id}/pause``, ``POST /v1/agents/{id}/resume``,
  ``POST /v1/agents/{id}/archive``. The pause / resume pair flips
  ``agent.yml``'s ``state`` field; the reconciler picks the change
  up on its next tick and stops or starts the worker. Archive
  pauses first (so the worker exits cleanly + releases sqlite-WAL
  file handles), then drops an ``archive.flag`` sentinel; the
  reconciler then moves the agent dir into
  ``~/.puffo-agent/archived/<id>-ws-<timestamp>``. All three are
  idempotent and ownership-gated — only the agent's operator can
  call them; non-owners get 403, missing IDs get 404.

  Tests in ``tests/test_pause_resume_archive.py``: state-flip
  parity, idempotent-already-{paused,running} note, 403 on
  non-owner, 404 on unknown id, archive writes the flag even when
  the agent was already paused, archive non-owner doesn't drop a
  stray flag.

- ``role`` + ``role_short`` fields on ``AgentConfig`` and the local
  bridge profile-edit path. Mirrors the
  [puffo-server identity_role migration](https://github.com/puffo-ai/puffo-server/pull/50)
  that surfaces the same fields on the server identity profile.
  ``role`` is the long form (≤140 chars, recommended shape
  ``<short>: <description>``); ``role_short`` is the chip label
  clients render in member lists (≤32 chars). Both default to empty
  string; existing agent.yml files load with empty values and don't
  need a fresh write.

  Plumbed across:
  * ``AgentConfig`` load/save preserves the two fields.
  * ``PATCH /v1/agents/{id}/profile`` accepts ``role`` + ``role_short``,
    validates lengths, rejects ``role_short`` without ``role``
    (mirrors the server-side 400), updates agent.yml, and best-effort
    syncs to ``PATCH /identities/self`` via the existing
    ``_sync_agent_profile`` helper. Sync failures log but don't
    block the local write.
  * ``POST /v1/agents`` (bridge create) accepts the same two fields
    and fires a best-effort post-create sync so a freshly-provisioned
    agent's server-side identity profile carries its role from the
    start.
  * ``puffo-agent agent create`` CLI gained ``--role`` /
    ``--role-short`` flags. Length and "short without role"
    validation matches the bridge.

  ``role_short`` defaults to the server-side derive on save:
  ``"coder: main puffo-core coder"`` → ``"coder"``. A local mirror
  of that derive in the bridge handler + CLI keeps agent.yml
  consistent with what the server stores without an extra GET. The
  agent guides include a unit test pinning both mirrors against the
  server contract.

### Fixed

- ``_profile_summary`` returns the full ``# Soul`` section body
  instead of just the first non-blank line. The web client's
  AgentsPane card has a "▸ Soul" expand toggle that revealed only
  one sentence of what the operator typed; the helper now walks
  from the matching heading (``# Soul`` / ``# Description`` /
  ``# About`` / ``# Summary``) to the next top-level heading or
  EOF, preserves sub-headings inside the body, and trims leading
  + trailing blank lines. Round-trip with ``_update_profile_summary``
  (which already wrote the full body) is now lossless.

- README and the new "Agent identity" section now document the
  five operator-editable fields (display_name, avatar_url, role,
  role_short, soul) and how they map onto ``agent.yml`` +
  ``profile.md``. The ``# Soul`` body is what the LLM reads every
  prompt; sub-headings inside it travel along.

## [0.7.5] — 2026-05-12

### Added

- Auto self-introduction after accepting an invite. When the agent
  accepts an ``invite_to_channel`` (auto-accepted from an operator-
  matched signer, or accepted via the operator's ``y`` DM reply),
  the daemon enqueues a synthetic ``[puffo-agent system message]``
  envelope on the invited channel asking the agent to post a short
  intro (default English, 2-3 sentences) via its normal
  ``mcp__puffo__send_message`` path. For ``invite_to_space``, the
  daemon resolves the space's auto-General channel via ``GET
  /spaces/<id>/channels`` (the row with ``is_public=true``) and
  fires the same nudge there — General is the one channel the agent
  gets auto-fanned-out membership on when accepting a space invite,
  so it's the only channel it can immediately post into. Existing
  ``profile.md`` personality shapes the wording.

  Wiring:
  * New sqlite table ``channel_intro_prompted(channel_id PK,
    prompted_at)`` on the per-agent ``messages.db`` gates the nudge
    so a daemon restart or server-side invite redelivery can't fire
    a second intro on the same channel.
  * New sqlite table ``channel_space_map(channel_id PK, space_id,
    learned_at)`` records the channel→space mapping discovered
    out-of-band by the General lookup. ``lookup_channel_space``
    (used by the MCP ``send_message`` tool) checks this map first,
    then falls back to the historical ``messages``-table inference.
    Without this, the agent's first send into the freshly-joined
    channel would 404 the data-service lookup (no prior messages on
    that channel) and fall back to ``agent.yml``'s configured
    ``space_id`` — the WRONG space when the agent has just joined
    a different one.
  * General lookup retries on a sleep-first schedule
    (0.5 / 1 / 3 / 6 / 12 / 24 / 24 seconds, ~70s total) because
    the server has a tight race between the ``accept_space_invite``
    POST returning and the ``channel_memberships`` row that gates
    ``GET /spaces/<id>/channels`` being committed. Past ~70s the
    daemon gives up silently — missing the intro is preferable to
    spinning forever.
  * ``_channel_name_cache`` is warmed from the same ``/channels``
    response so the ``_resolve_channel_name`` call inside the
    nudge becomes a cache hit instead of a duplicate HTTP round-
    trip.

- Every message fed to the agent now carries the sender's
  ``display_name`` alongside the slug, rendered as two distinct
  fields in the user block:

  ```
  - sender: Alice Wong
  - sender_slug: alice-0001
  - sender_type: human
  ```

  ``sender`` is the human-readable handle the LLM uses to address
  peers in prose; ``sender_slug`` stays the structural identifier
  for @-mentions and ``send_message`` routing. When the server has
  no display_name on file, ``sender`` echoes the slug so the field
  is always populated. Resolution piggy-backs on the existing
  per-session display-name cache (one ``/identities/profiles?slugs=``
  call per distinct sender per session; subsequent messages from
  the same sender are free).

### Changed

- ``- sender: <slug> <email>`` rendering removed from the user
  block; ``sender_email`` was hardcoded to ``""`` everywhere it
  was populated and just cluttered the prompt.

## [0.7.4] — 2026-05-12

### Fixed

- cli-local: MCP server was crashing on agent startup with
  `ModuleNotFoundError: No module named 'mcp'` on macOS / Linux
  installs where `mcp` lives in real-user user-site (e.g. installed
  via `pip install --user` or as a transitive dep of `pip install
  -e .`). The adapter rewrites `HOME` (and `USERPROFILE`) on the
  claude subprocess so each agent has its own `~/.claude`; the MCP
  subprocess claude spawns inherited that HOME, and Python computed
  user-site relative to it (`<per-agent-home>/Library/Python/...`),
  landing on an empty per-agent directory. Result: every
  `mcp__puffo__*` tool went missing and the agent fell back to
  plain-text replies. Now `puffo_core_mcp_env` injects
  `PYTHONUSERBASE` pointing at the daemon's real user base
  (captured via `site.getuserbase()` at module load time, before
  any HOME mutation), which the spawned MCP subprocess uses to
  resolve user-site independent of HOME — `.pth` files processed,
  editable installs honoured. cli-docker is excluded (container
  has its own Python tree with deps baked in, so a host path would
  be meaningless inside it); Windows is unaffected in practice
  because Windows user-site reads `APPDATA`, not `HOME`, but the
  `PYTHONUSERBASE` injection is harmless there.

## [0.7.3] — 2026-05-11

### Added

- New MCP tool `mcp__puffo__get_thread_history(root_id, limit,
  since, before, after)` — root post + every reply in one thread,
  oldest-first. Companion to the restructured
  `get_channel_history`; agents drill into a thread only when
  `get_channel_history` shows a non-zero reply count worth reading.
- `[puffo-agent system message]` prefix on control messages the
  runtime injects into the agent's claude-code transcript (today
  used for the rate-limit retry kick; the prefix is documented in
  CLAUDE.md so the agent recognises future control messages
  without another prompt update).

### Changed

- Message processing is now thread-batched instead of per-message.
  Each root message id (`envelope_id` of the thread root) is a single
  slot in the priority queue; new messages on the same thread coalesce
  into the existing slot, a higher-priority arrival bumps the slot but
  preserves the batch cursor, and the consumer drains the full batch
  in one `on_message_batch` dispatch. The agent reads the whole thread
  and decides on its own who to reply to — no trigger-message concept.
- Cross-restart dedup: a new `thread_processing_state(root_id PK,
  last_processed_sent_at)` table records the last `sent_at` drained
  per thread. On startup the runtime skips anything at or below that
  cursor so a crash mid-batch doesn't replay the whole thread.
- Pre-dispatch jitter (0.0–1.5s) before each batch is sent to the
  agent. When multiple agents on the same host get activated by one
  broadcast message, unblocked dispatch sends them all into the
  claude-code API simultaneously and trips its rate limit; the
  random sleep desynchronises them. Each agent picks its own delay
  independently — no host-wide coordination needed.
- Status telemetry follows the thread-batch lifecycle. The first
  message in a batch still gets a single `/processing/start`
  (yellow dot lands there), but the per-message `/end` fan-out is
  replaced by a single `/messages/processing/end:batch` call that
  flips every message in the batch to green at once. Requires the
  puffo-server endpoint added in puffo-server#46 (deployed
  2026-05-11).
- `mcp__puffo__get_channel_history` now returns **root posts only**
  with a per-thread `(N replies)` annotation instead of inlining
  every reply. One busy thread no longer eats the whole channel's
  view. Same call also accepts `since=<envelope_id>` / `before=<ms>`
  / `after=<ms>` filters for incremental polling.
- Both `get_channel_history` and `get_thread_history` distinguish
  "unknown channel/thread" (HTTP 404 → "(no such channel: …)" /
  "(no such thread: …)") from "known but window-empty after
  filters" (HTTP 200 + `[]` → "(no … in the requested window)").
  Agents can tell a typoed id from a quiet polling window.

### Fixed

- Thread-batched dispatch could feed the agent the same envelope
  multiple times in one batch when the server's pending-message
  redelivery overlapped with live WS delivery (most easily reproduced
  after a daemon restart). `MessageStore.store` already used
  `INSERT OR IGNORE`, but the in-memory `_ThreadEntry.messages`
  batch had no envelope-level dedup. Now `_admit_thread_message`
  drops the append when the incoming `envelope_id` is already in the
  pending batch.
- A duplicate WS delivery that landed during a successful dispatch
  was being added to a fresh batch and re-fed to the agent on the
  next turn — observed as the same `envelope_id` appearing twice
  across consecutive claude-code turns (with a system-reminder
  injected between them). The handle_envelope cursor check couldn't
  catch it because `mark_thread_processed` runs *after* the dispatch
  finishes, and the in-batch dedup couldn't catch it because the
  consumer empties `entry.messages` to claim the batch. Adds a per-
  thread `dispatching_ids` set populated at claim time and consulted
  at the top of `_admit_thread_message`; cleared after the cursor
  advances on successful dispatch.
- Belt-and-suspenders: the consumer now dedupes `batch` by
  `envelope_id` one final time immediately before invoking
  `on_message_batch`, logging a warning if anything is dropped. The
  agent must never see the same envelope twice in one turn, even if
  some upstream race we haven't characterised slips through.
- `AgentAPIError` retry was duplicating the user input in the
  agent's claude-code transcript on every retry. When claude-code
  returns `API Error: Server is temporarily limiting requests`, the
  consumer re-enqueues the batch and `handle_message_batch` runs
  again — and the cli adapter `--resume`s the same claude-code
  session, so the same user message got appended to the transcript
  once per retry. A receiver who saw `×4` was hitting the rate
  limit four times before succeeding, with each attempt being a
  legitimate retry of one real wire delivery. New behaviour:
  preserve `--resume`, send a kick-only "session errored on rate
  limiting, please resume processing" instead of re-appending the
  batch. The cli adapter falls back to the full payload only when
  `_ResumeFailed` was caught and a fresh claude-code session was
  spawned (so the transcript has no original input to resume from).
  Plumbed via a new `Adapter.run_retry_turn(kick_text,
  fallback_user_message, ctx)` and a parallel
  `_consume_queue(on_api_error_retry=...)` callback. Capped at 3
  retries per batch; on exhaustion the cursor stays put so the
  failed envelopes remain readable via `get_channel_history` on
  the agent's next normal turn.
- cli-docker now auto-recreates a reused container when its
  `/opt/puffoagent-pkg` bind mount no longer resolves to the host
  path that contains `puffo_agent` (typical cause: the operator
  reinstalled puffo-agent from a different path, e.g. moved off
  `puffo-core-han-group/agent` to the standalone repo). Without
  this, `python3 -m puffo_agent.mcp.puffo_core_server` inside the
  container would fail with `ModuleNotFoundError`, claude-code's
  MCP subprocess wouldn't initialise, and every puffo MCP tool
  surfaced as "No such tool available". Detected by `docker exec
  test -f /opt/puffoagent-pkg/puffo_agent/__init__.py`.
- `AgentAPIError` retry path was clobbering mid-dispatch arrivals.
  When the dispatch failed AND a new message had landed on the same
  thread during the failed dispatch (admitted through the reopen
  branch of `_admit_thread_message`), the handler set
  `entry.messages = batch` and silently dropped the new message.
  The retry now dedupe-prepends the failed batch to whatever is
  already in `entry.messages` so the next attempt carries both.
- cli-local adapter silently dropped MCP servers passed via
  `--mcp-config`. claude-code gates MCP registration on the per-
  project trust dialog stored in `~/.claude.json`, and the dialog has
  no TUI surface under `--input-format stream-json`, so it never
  resolved. Switched the `bypassPermissions` permission-mode to emit
  `--dangerously-skip-permissions` (which also bypasses the trust
  dialog) instead of `--permission-mode bypassPermissions` (which
  only bypasses per-tool prompts). cli-docker already used the right
  flag; this aligns cli-local.

## [0.7.2] — 2026-05-10

First public PyPI release.

### Added

- Trusted Publishing workflows for PyPI and TestPyPI under
  `.github/workflows/`.

### Fixed

- `/spaces/<id>/events` pagination used the wrong query param
  (`cursor=` instead of `since=`); axum's `Query` extractor silently
  ignored it, so the agent's `_resolve_channel_name` loop fetched
  page 1 forever — pinning a worker's CPU and growing its WS receive
  queue until the host OOM'd. Also added a defensive strict-advance
  guard in the same loop and in the MCP `list_channels` tool, so a
  future server-side regression that echoes the same cursor back
  bails instead of spinning.

[Unreleased]: https://github.com/puffo-ai/puffo-agent/compare/v0.8.3...HEAD
[0.8.3]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.3
[0.8.2]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.2
[0.8.1]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.1
[0.8.0]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.0
[0.7.5]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.5
[0.7.4]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.4
[0.7.3]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.3
[0.7.2]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.2
