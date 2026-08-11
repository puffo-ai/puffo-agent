# Agent Foundation 2.0 Test Release

- **Candidate:** `puffo-agent==2.0.0a1`
- **Status:** staging and TestPyPI validation only
- **Stable release:** not authorized by this merge

This candidate supersedes the incorrectly numbered TestPyPI-only `1.3.0a1`
artifact. No stable `1.3.0` package was published.

This document freezes the release decisions and evidence expected after the
Agent Foundation integration branch merges. It is intentionally narrower than
the complete product roadmap.

## Decisions

1. Merge the Agent Foundation implementation to `main`, but publish it first as
   `2.0.0a1` through the manual TestPyPI workflow. Do not create a stable GitHub
   release or publish `2.0.0` to production PyPI yet.
2. Validate the candidate against staging. A green merge CI run is necessary
   but is not evidence of live Server, harness, or upgrade compatibility.
3. Channel encryption policy and plaintext-channel behavior are owned by Han's
   separate [PR #212](https://github.com/puffo-ai/puffo-agent/pull/212). This
   candidate does not merge, duplicate, or claim that work.
4. The Server and local/PyPI Agent are one release track. AIM/E2B cloud template
   promotion and migration of existing cloud Agents are a separate release
   track.
5. Runtime execution content remains local. Only fixed-vocabulary Runtime Event
   metadata may be uploaded.
6. No more broad refactoring, feature additions, or test-suite expansion belongs
   in this candidate. Fix only failures that block the acceptance cases below.

## TODO Before Stable 2.0.0

- Forward-port the Windows Claude Code shim-spawn behavior from
  [PR #224](https://github.com/puffo-ai/puffo-agent/pull/224) to the new Claude
  Driver, then verify it on a real Windows host.
- Merge and deploy
  [puffo-server PR #273](https://github.com/puffo-ai/puffo-server/pull/273),
  including staging validation before production rollout.
- Query production `agent_status.runtime` and decide how to handle active
  Docker Codex and retired direct-runtime configurations. Never silently move a
  Docker Agent onto the host security boundary.
- Add the immutable Agent SHA to the AIM/E2B per-harness template release,
  validate candidate templates in staging, and define migration for existing
  pinned cloud Agents before calling cloud Agent support generally available.
- Add stable-release evidence and change the package version, changelog, and
  README from `2.0.0a1` to `2.0.0` only after the gates above close.

The following product work remains deferred rather than release-blocking:
keyless invitation approval, complete keyless DM trust management, Runtime
Event UI, encrypted remote runtime-output streaming, reactions, Hermes, Gemini,
ACP, and plaintext DMs.

## Staging Acceptance

Run the smallest set that exercises the integrated behavior on the final
candidate SHA:

1. Install `2.0.0a1` from TestPyPI in a clean environment and confirm the CLI
   starts:

   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ puffo-agent==2.0.0a1
   puffo-agent --help
   ```

2. Upgrade a copied `1.2.0` state directory. Confirm `agent.yml`, `daemon.yml`,
   profile, memory, workspace, keys, message history, and supported session
   references remain usable.
3. Run one real Claude Code turn and one real Codex app-server turn against
   staging, including Puffo MCP tool discovery and an explicit send.
4. Run the three coordination cases on fresh channels:
   - five Agents count `1,2,3,4,5`;
   - the fourth Agent repeats `1`, yielding `1,2,3,1,5`;
   - five Agents take five turns each without duplicate or abandoned progress.
5. Exercise one held draft through context recovery and a model-owned decision:
   revise, wait with a reminder, remain silent, or explicitly send anyway.
6. Deliver pending messages across more than one space/target in one Inbox turn
   and confirm each response is routed to the intended channel, thread, or DM.
7. Restart during pending Inbox and reminder work. Confirm persisted messages
   are not acknowledged before turn admission and one-shot reminders do not
   duplicate after reconstruction.
8. Inspect staging Runtime Events and logs: no assistant output, reasoning,
   message content, tool payload, credential, or Inbox body may leave the local
   runtime-event outbox.

Channel encryption/plaintext behavior is not part of this candidate's
acceptance result. Browser UI validation is one final smoke after the CLI and
runtime cases pass; it is not the primary harness for debugging them.

## Publishing

Run `.github/workflows/publish-testpypi.yml` manually from the final merged SHA.
Do not publish a GitHub Release for this candidate: the production workflow is
reserved for a stable `X.Y.Z` version whose `vX.Y.Z` tag matches
`pyproject.toml`.
