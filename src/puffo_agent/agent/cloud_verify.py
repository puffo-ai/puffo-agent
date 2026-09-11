"""Readiness checks for a cloud agent — is it actually able to serve?

The checks are **pure**: they take a dict of collected facts and return
verdicts. Collecting those facts needs a sandbox connection and therefore the
E2B SDK; evaluating them needs neither, which is what makes this testable
without credentials and reusable from anywhere that can gather the same facts
(a CLI today, a server-side `--migrate` later).

Every check here exists because it failed at least once during the subscription
build-out on 2026-09-10/11. This is a list of things that have actually gone
wrong, not a list of things that could.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: A fact the collector could not determine. Distinguished from a falsy value:
#: "I could not look" is not "it is absent", and conflating them turns a broken
#: collector into a clean bill of health.
UNKNOWN = object()


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool | None  # None = could not determine
    detail: str
    catches: str

    @property
    def mark(self) -> str:
        return {True: "ok", False: "FAIL", None: "????"}[self.ok]


@dataclass(frozen=True)
class Check:
    name: str
    #: What this catches — printed on failure so the reader learns the failure
    #: mode, not just that a box is red.
    catches: str
    run: Callable[[dict[str, Any]], tuple[bool | None, str]]


def _fact(facts: dict[str, Any], key: str) -> Any:
    return facts.get(key, UNKNOWN)


def _binding_running(f):
    v = _fact(f, "state")
    if v is UNKNOWN:
        return None, "no binding state collected"
    return v == "running", f"state={v!r}"


def _expected_template(f):
    got, want = _fact(f, "template_id"), _fact(f, "expected_template")
    if got is UNKNOWN:
        return None, "no template id collected"
    if want is UNKNOWN or not want:
        return True, f"{got} (nothing to compare against)"
    return got == want, f"booted {got}, expected {want}"


def _auth_mode_matches(f):
    got, want = _fact(f, "auth_mode"), _fact(f, "expected_auth_mode")
    if got is UNKNOWN:
        return None, "agent.yml not read"
    if want is UNKNOWN or not want:
        return True, f"{got!r} (no expectation given)"
    return got == want, f"agent.yml says {got!r}, expected {want!r}"


def _no_virtual_key(f):
    mode = _fact(f, "auth_mode")
    if mode is UNKNOWN:
        # The mode decides what a key *means* here, so not knowing it makes this
        # unanswerable. Reporting ok would be worst-case wrong: an unread
        # agent.yml is exactly the state where auth_mode was silently absent.
        return None, "agent.yml not read — cannot judge whether a key is correct"
    if mode != "subscription":
        return True, "not a subscription agent — a key is correct here"
    key = _fact(f, "api_key")
    if key is UNKNOWN:
        return None, "agent.yml not read"
    return not key, "api_key is empty" if not key else "a virtual key was minted"


def _credential_reaches_the_cli(f):
    mode = _fact(f, "auth_mode")
    tok, base, api = (_fact(f, k) for k in ("child_has_token", "child_has_base_url", "child_has_api_key"))
    if tok is UNKNOWN:
        return None, "child process env not read (agent may be idle)"
    if mode is UNKNOWN:
        return None, "agent.yml not read — cannot judge which credential is correct"
    if mode == "subscription":
        ok = tok and not base and not api
        return ok, f"token={bool(tok)} base_url={bool(base)} api_key={bool(api)}"
    ok = bool(api) and not tok
    return ok, f"api_key={bool(api)} token={bool(tok)}"


def _talks_to_the_right_upstream(f):
    mode = _fact(f, "auth_mode")
    anth, gw = _fact(f, "conns_anthropic"), _fact(f, "conns_gateway")
    if anth is UNKNOWN or mode is UNKNOWN:
        return None, "no connection sample" if anth is UNKNOWN else "agent.yml not read"
    if anth < 0 or gw < 0:
        # -1 is the probe saying a host would not resolve, not "zero sockets".
        return None, "upstream host did not resolve in the sandbox"
    where = f"anthropic={anth} gateway={gw}"
    if anth == 0 and gw == 0:
        # An agent between turns holds no upstream socket at all. That is the
        # normal resting state, not evidence of anything -- reporting it as a
        # failure made a freshly-resumed healthy agent look misconfigured.
        # This check can only ever convict traffic it can see.
        return None, where + " — no upstream sockets; agent idle, nothing to judge"
    if mode == "subscription":
        return gw == 0, where
    return gw > 0, where


def _profile_is_not_a_stub(f):
    n = _fact(f, "profile_bytes")
    soul = _fact(f, "profile_has_soul")
    if n is UNKNOWN:
        return None, "profile.md not read"
    if n <= 512:
        return False, f"{n}B — still the created-but-never-seeded stub"
    return bool(soul), f"{n}B, soul section {'present' if soul else 'MISSING'}"


def _memory_present(f):
    n = _fact(f, "memory_files")
    if n is UNKNOWN:
        return None, "memory tree not read"
    return n > 0, f"{n} file(s) excluding .git"


def _no_startup_errors(f):
    errs = _fact(f, "log_errors")
    if errs is UNKNOWN:
        return None, "log not read"
    return not errs, "clean" if not errs else "; ".join(f"{k}×{v}" for k, v in errs.items())


def _bridge_connected(f):
    """Sockets to the RELAY specifically.

    Counting every established connection reported "connected" for an agent
    with a live LLM connection and a dead bridge — the exact shape of the
    failure this check exists to catch.
    """
    n = _fact(f, "conns_relay")
    if n is UNKNOWN:
        return None, "bridge state not determined"
    if n < 0:
        return None, "relay host not resolvable from agent.yml"
    noise = _fact(f, "bad_frame_count")
    extra = "" if noise in (UNKNOWN, 0) else f" ({noise}× BAD_FRAME — known list_invites noise)"
    return n > 0, (f"{n} socket(s) to the relay" if n else "no relay socket") + extra


CHECKS: tuple[Check, ...] = (
    Check("binding running", "a create that silently failed", _binding_running),
    Check("expected template", "an override not applied, or a stale ledger pin", _expected_template),
    Check("auth_mode matches", "the provisioner→agent.yml wire being broken", _auth_mode_matches),
    Check("no virtual key", "AIM minting a key a subscription agent must not have", _no_virtual_key),
    Check("credential reaches the CLI", "a leftover ANTHROPIC_BASE_URL silently outranking the plan token", _credential_reaches_the_cli),
    Check("talks to the right upstream", "believing the mode without checking the traffic", _talks_to_the_right_upstream),
    Check("profile is seeded", "an agent created but never given its persona", _profile_is_not_a_stub),
    Check("memory present", "an agent that will answer from an empty brain", _memory_present),
    Check("no startup errors", "credential-refresh and CLI-exit failures", _no_startup_errors),
    Check("bridge connected", "an agent that can think but not speak", _bridge_connected),
)


def evaluate(facts: dict[str, Any]) -> list[CheckResult]:
    """Run every check. Always runs all of them — a half-configured agent
    usually fails several at once, and the pattern is the diagnosis."""
    out = []
    for c in CHECKS:
        try:
            ok, detail = c.run(facts)
        except Exception as exc:  # a broken check must not hide the others
            ok, detail = None, f"check raised {type(exc).__name__}"
        out.append(CheckResult(c.name, ok, detail, c.catches))
    return out


def summarize(results: list[CheckResult]) -> tuple[int, int, int]:
    """(passed, failed, undetermined)."""
    return (
        sum(1 for r in results if r.ok is True),
        sum(1 for r in results if r.ok is False),
        sum(1 for r in results if r.ok is None),
    )
