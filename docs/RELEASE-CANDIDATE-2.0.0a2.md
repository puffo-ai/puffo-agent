# Agent Foundation 2.0 Test Release

- **Candidate:** `puffo-agent==2.0.0a2`
- **Status:** staging and TestPyPI validation only
- **Stable release:** not authorized by this merge

This candidate supersedes the TestPyPI-only `2.0.0a1` candidate. The earlier
candidate and its audit remain historical evidence; `a2` contains the
compatibility and authorization fixes found during that validation.

The ordered implementation and release backlog is tracked in
[`roadmap/agent-foundation/STABLE-2.0-READINESS.md`](../roadmap/agent-foundation/STABLE-2.0-READINESS.md).

## Candidate Delta

- Restores Docker Codex and Windows Claude shim support on the Driver runtime.
- Restores daemon stop, status, telemetry, and optional context-baseline
  compatibility for existing installations.
- Adds durable operator authorization for keyless foreign DMs and space
  invitations.
- Exposes one host-wide collaboration directory as `shared/` inside every
  private Agent workspace while preserving existing conflicting content.

## Decisions

1. Publish `2.0.0a2` only through the manual TestPyPI workflow. Do not create a
   stable GitHub release or publish `2.0.0` to production PyPI yet.
2. Validate the exact candidate commit against staging. Green CI is necessary
   but is not evidence of live Server, harness, Windows, or upgrade behavior.
3. Channel encryption and plaintext-channel policy remain outside this
   candidate.
4. Server/local Agent release and AIM/E2B template promotion remain separate
   tracks with explicit immutable commit pins.
5. Runtime execution content remains local. Only fixed-vocabulary Runtime Event
   metadata may be uploaded.
6. Limit further candidate changes to acceptance blockers and their narrow
   boundary tests.

## TODO Before Stable 2.0.0

- Run a real Windows Claude Code turn through an npm shim.
- Validate keyless invitation and foreign-DM approval/rejection, including
  restart and replay, against the real staging bridge.
- Verify the required Puffo Server changes are merged and deployed, then audit
  staging Runtime Event privacy.
- Inventory production runtime configurations and explicitly migrate, pin, or
  retire every active Docker Codex and legacy direct-runtime Agent.
- Pin and validate AIM/E2B templates from the immutable candidate commit.
- Record upgrade, harness, coordination, recovery, multi-target Inbox, restart,
  and privacy evidence against one final candidate SHA.
- Only after those gates close, update package metadata from `2.0.0a2` to
  `2.0.0`, tag it, and run the production publishing workflow.

Runtime Event UI, encrypted remote runtime-output streaming, reactions,
Hermes, Gemini, ACP, and plaintext DMs remain separate product work.

## Staging Acceptance

1. Clean-install the candidate from TestPyPI and confirm the CLI starts:

   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ puffo-agent==2.0.0a2
   puffo-agent --help
   ```

2. Upgrade a copied `1.2.0` state directory. Confirm configuration, profile,
   memory, workspace, keys, message history, supported session references, and
   the managed `workspace/shared` link remain usable.
3. Run one real Claude Code turn and one real Codex app-server turn, including
   Puffo MCP discovery and an explicit send.
4. Run the three coordination cases on fresh channels: `1,2,3,4,5`; the fourth
   Agent repeats `1`; and five Agents each take five turns without duplicates
   or abandoned progress.
5. Exercise held-draft recovery through revise, wait with a reminder, silence,
   and the rare explicit send-anyway decision.
6. Deliver pending messages across multiple spaces and targets in one Inbox
   turn and verify routing to the intended channel, thread, or DM.
7. Restart during pending Inbox, reminder, keyless DM approval, and invitation
   approval work; verify persistence without duplicate side effects.
8. Inspect staging Runtime Events and logs. Assistant output, reasoning, message
   content, tool payloads, credentials, and Inbox bodies must remain local.

Browser validation is the final product smoke after CLI and runtime acceptance;
it is not the primary debugging harness.

## Publishing

Run `.github/workflows/publish-testpypi.yml` manually from the final merged SHA.
Do not publish a GitHub Release for this candidate.
