# Agent Foundation 2.0 Stable Readiness

## Stable Release Decision

On 2026-08-23, the product owner explicitly authorized publication of
`puffo-agent==2.0.0` from the hardened `2.0.0a25` source. The external evidence
gaps retained below were accepted as post-release follow-up risks; they were not
retroactively marked complete. This document remains the historical readiness
backlog and evidence inventory rather than the current package status.

This is the execution backlog for moving `puffo-agent==2.0.0a2` toward a
stable `2.0.0` release. The release contract and acceptance cases remain in
[`docs/RELEASE-CANDIDATE-2.0.0a2.md`](../../docs/RELEASE-CANDIDATE-2.0.0a2.md).

The reviewed, source-grounded inventory of every item below against the frozen
Agent source and local git evidence is in
[`roadmap/agent-foundation/STABLE-2.0-SOURCE-AUDIT.md`](STABLE-2.0-SOURCE-AUDIT.md).
Treat that audit as authoritative for classifications and evidence; this table
tracks status and evidence links.

The goal is readiness evidence and the smallest required fixes. This branch is
not a new feature integration branch.

## Remaining Work

| ID | Status | Remaining gap | Owner / repository | Stable blocker | Completion evidence |
|---|---|---|---|---|---|
| A1 | Local code complete at `8a56124` | Real Windows execution is not locally verified. | Puffo Agent + Windows QA | Yes | Run one real Windows Claude turn through an npm shim and record the immutable Agent commit. |
| K1 | Local Agent and Server feature work complete at `ea83bc2` / `393a1af` | Real keyless bridge acceptance is not locally verified. | Puffo Agent + Puffo Server + staging | Yes for keyless GA | Exercise external invitation approval/rejection and foreign-DM allow/block through the real bridge, including restart/replay, against immutable candidate SHAs. |
| S1 | External evidence pending | Runtime Event Server merge/deploy state and staging privacy behavior must be reverified. | Puffo Server | Yes | Server change merged and deployed, and a real Agent run confirms that only fixed-vocabulary metadata reaches the Server. |
| O1 | External evidence pending | Production `agent_status.runtime` has not been inventoried for active Docker Codex or retired direct-runtime configurations. | Release operations | Yes | Inventory recorded and every active runtime class has an explicit migration, pin, or retirement decision. No Docker Agent silently moves to the host boundary. |
| C1 | External evidence pending | AIM/E2B templates are not yet pinned and promoted from an immutable Agent commit for each supported harness. | Cloud Agent / AIM | Yes for cloud GA | Candidate templates pass staging, existing cloud Agent pins have a migration plan, and promotion records the exact Agent commit. |
| V1 | External evidence pending | The final candidate SHA does not yet have one consolidated staging evidence record for upgrade, real harness, coordination, recovery, multi-target Inbox, restart, and privacy behavior. | Puffo Agent + staging | Yes | All acceptance cases in the release contract are recorded against one immutable candidate SHA. |
| R1 | Intentionally deferred | Stable package metadata and publishing remain disabled. | Puffo Agent release | Yes | Only after A1, K1, S1, O1, C1, and V1 close: set `2.0.0`, update changelog and README, tag `v2.0.0`, and run the production workflow. |

## Evidence Already Available

- `2.0.0a1` was built and clean-installed from TestPyPI from the merged
  candidate commit. Any code change after that commit requires a new candidate
  version and a fresh install check.
- Agent Foundation and its release correction are on `main` through Agent PRs
  #225 and #229.
- Agent PR #224 documents the historical Windows failure and old Adapter fix;
  the new Driver forward-port is now present locally at `8a56124`.

These facts reduce repeated discovery work; they do not replace final staging
evidence.

## Execution Order

1. **Platform evidence:** complete the real-Windows A1 turn and real-bridge K1
   invitation/DM scenarios against immutable Agent and Server commits.
2. **Server dependency:** merge and deploy S1 before privacy acceptance.
3. **Release inventory:** complete O1 before choosing migration defaults.
4. **Candidate validation:** run V1 on the exact Agent and Server candidate
   commits. Use CLI/runtime tests for diagnosis and one browser smoke at the
   end.
5. **Cloud track:** complete C1 before calling cloud Agent support generally
   available.
6. **Stable release:** perform R1 only after every applicable gate is closed.

## Explicitly Out Of Scope

- Channel encryption and plaintext-channel policy are owned by Han's Agent PR
  #212 and must not be duplicated here.
- Keyless invitation/DM **operator authorization** was the bounded K1
  requirement for this workstream and is locally implemented; real bridge
  acceptance remains in the table above. See the
  [source audit](STABLE-2.0-SOURCE-AUDIT.md) for the original missing behavior
  boundary. The broader keyless DM trust-management surface, Runtime Event UI,
  encrypted remote runtime-output streaming, reactions, Hermes, Gemini, ACP,
  and plaintext DMs remain separate product work.
- Broad refactoring and test-suite expansion are not readiness work. Add only
  tests that guard a changed boundary or a release acceptance failure.

## Branch Discipline

- Keep one commit or review unit per gap; do not mix Agent code, release
  operations, and cloud-template work.
- Treat the release contract as canonical. Update this backlog with status and
  evidence links rather than copying new acceptance rules into multiple files.
- Preserve existing `1.2.0` user state during upgrade: configuration, profile,
  memory, workspace, keys, message history, and supported session references.
- Do not publish stable artifacts from this branch.
