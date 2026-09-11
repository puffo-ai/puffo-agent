# Agent Foundation 2.0 Compatibility Matrix

Source-grounded inventory of pre-2.0 Web-visible contracts affected by the
Agent Foundation 2.0 migration, their original failure evidence, implemented
repairs, and bounded acceptance evidence.

## Purpose and method

- Frozen goal: `loop-engineering/runtime/agent-foundation-2.0/goal.md`
  (referenced, not modified).
- Findings F001-F006:
  `/Users/glimmer/Desktop/projects/puffo.ai/slock-agent/research/puffo-agent-staging-findings/`.
- The detailed pre-repair source blocks remain frozen at the diagnosis snapshots
  so the failure mechanism stays auditable. Resolution claims are pinned
  separately to the current Agent and Server heads below.
- Git history was used only to recover specific pre-2.0 contracts (stop sentinel,
  `cli-docker + codex` admission/adapter/image/tests, status lifecycle wiring,
  telemetry projection, baseline fallback introduction).
- Classifications are restricted to: `restored`, `intentionally deferred`,
  `not a regression`, `missing`.

## Snapshot used by this matrix

| Surface | Snapshot |
|---|---|
| Agent diagnosis snapshot | `dd5f4ef517decaa814ffb9bc27b7c2d2b53efc89` |
| Server diagnosis snapshot | `393a1af6f54b9f44e0711437bd787ce0f093e80b` |
| Agent repaired head | `df635e9` (`feat/agent-foundation-stable-readiness`) |
| Server repaired head | `ae0f238` (`feat/keyless-agent-authorization`) |
| Web product-validation snapshot | `bc5db876` (`origin/main`, local compatibility worktree) |
| Integration commit audited | `0d9f44d` "Integrate Agent Foundation across native and keyless cloud runtimes (#225)" |
| Findings source | `puffo-agent-staging-findings` snapshot 2026-08-11; staging Server `sha-7ad4698b959a`; package `puffo-agent==2.0.0a1` |

All Agent path citations below are relative to the Agent worktree root; Server
citations are under `server/` in the Server worktree. Line numbers in the
diagnosis blocks refer to the recorded diagnosis snapshots.

## Resolution status

| Finding | Status | Bounded evidence |
|---|---|---|
| F001 introduction / baseline | Restored | Missing local row remains `None`; JSON `null`, authoritative `0`, and positive sequence values stay distinct. Server establishes a numeric baseline only inside the first successful coordinated send. Five Agents joining both empty-history and pre-history channels each introduced themselves once. |
| F002 busy / activity strip | Restored | Global Inbox turns open and close the legacy Server status lifecycle. Real Web acceptance showed `Processing` during work and `Idle` after completion for all five Agents. Daemon-local membership/reminder envelopes no longer call message-processing endpoints for nonexistent message IDs. |
| F003 Docker Codex | Restored with residual live-provider risk | `cli-docker + openai + codex` is admitted and wired through the Driver transport, isolated home, image, cleanup, and persisted-session paths. The bounded Docker smoke did not execute a live provider turn because the local Agent Core data/RPC dependency was unavailable. |
| F004 stop upgrade | Restored | Current daemon accepts both legacy timestamp-only and PID-scoped JSON stop sentinels; current CLI waits on the exact daemon PID. Focused old/new compatibility and bounded shutdown tests pass. |
| F005 logs / telemetry | Restored | A currently paired Python daemon delivered encrypted `turn_start`, bounded `assistant_text`, tool labels, and `turn_complete` to the current Web. Profile Log rendered the safe rows and nonzero Codex tokens (`14405` input / `599` output) while avatar and chat activity state returned to Idle. |
| F006 Activity / Files | Intentionally deferred | These surfaces did not have a complete pre-2.0 contract and remain product work. |

### Current local product evidence (2026-08-11)

- The legacy Python CLI created a device code from the real Agent home. Current
  Web `origin/main` opened `/link-machine`, approved **Agent Foundation
  Compatibility**, and the waiting CLI completed with the expected operator.
  The running daemon then established its control WebSocket and enabled the
  encrypted `agent.status` reverse channel. No old browser-to-daemon HTTP
  bridge was restored.
- A real channel turn showed `Processing` on the avatar and the chat activity
  strip, returned to `Idle`, and populated Profile Log with `Working`, safe
  tool labels, bounded assistant summaries, `Done`, and nonzero Codex token
  usage. Raw tool arguments, provider frames, and hidden reasoning were not
  exposed.
- In fresh channel `ch_08f31122-fad7-4f64-8f08-6b415b37a556`, all five Agent
  stores had no `channel_context_state` row immediately after membership, so
  all five baselines read as `None`. After each Agent's first successful
  coordinated introduction, the persisted values became one authoritative
  empty boundary (`0`) and four positive established boundaries
  (`2569`-`2572`). The Web showed five introductions exactly once and all five
  Agents settled to `Idle`.

### Cross-cutting reconnect recovery

Product acceptance exposed a separate compatibility failure: three Agents had
old `agent_runtime_state` rows left `running` after an earlier Runtime session
disappeared. Every event from their replacement session was rejected with
`409 invalid_turn_transition`, leaving Profile/runtime projection data stale.

Server commit `ae0f238` allows a `turn.started` from a different `session_ref`
to atomically replace an incomplete active turn. A second turn in the same
session remains invalid, reused turn references remain invalid, and late events
from the replaced session are fenced. No schema change or backfill is needed;
existing installations recover lazily on their next real turn.

Evidence:

- 14 focused Runtime Events tests pass, including replacement-session recovery
  and rejection of a late event from the old session.
- The unchanged local database contained three independently reproduced stale
  projections. A fresh five-Agent channel produced three explicit
  `runtime_event_session_takeover` transitions, Runtime Events for all 5/5
  Agents, five introductions, and five terminal `succeeded` projections.
- The same real browser run showed all five avatar states return to `Idle`; no
  runtime-event `409` remained after takeover.

---

## F001 — New-member introduction stalls at the visibility boundary

**Classification: `restored`**

Resolution: Agent merge `1f980ea` and Server merge `b1acd07` restore the
optional baseline contract. Missing local state is represented by no row and
serializes as JSON `null`; `0` remains an authoritative empty boundary. The
Server returns the established numeric value, which the daemon persists for
later sends and restarts. Membership/attach does not inspect encrypted history.

### Frozen compatibility contract

`context_baseline_seq` distinguished three states end to end:
- `None` (omitted) — no authoritative visible-history baseline established;
- `0` — an authoritative empty-channel baseline;
- a positive value — history through that sequence predates the Agent's context.

The 2026-07-26 design intent was to send JSON `null` for
`context_baseline_seq` in the new-member/no-visible-history case and let the
Server lazily establish the numeric baseline inside a successful coordinated
send. Freshness fields are daemon-owned; the model never supplies them.

### Pre-repair source behavior (frozen diagnosis)

- Agent storage for the baseline exists but has **no production writer**:
  `src/puffo_agent/agent/inbox_store.py:1024-1061` defines
  `get_context_baseline`/`set_context_baseline` over the `channel_context_state`
  table. Production call sites for `set_context_baseline` do not exist; the only
  callers are tests (`tests/test_message_store.py:1142-1143`,
  `tests/test_global_inbox_runtime.py:1035,1106`).
- The send path collapses the missing value to `0`:
  `src/puffo_agent/agent/send_coordinator.py:869-871`
  (`baseline = await self._baseline(...); if baseline is None: baseline = 0`).
- The request body always carries a numeric value:
  `src/puffo_agent/agent/send_coordinator.py:669-679`
  (`"context_baseline_seq": boundary.baseline` inside `freshness`).
- The Server DTO makes the field mandatory and has no `None` representation:
  `server/src/v2/agent_runtime_messages.rs:27-31`
  (`pub context_baseline_seq: i64`), validated non-negative at
  `server/src/v2/agent_runtime_messages.rs:81-85`.
- The Server send decision computes
  `effective_seen_seq = seen_seq.max(context_baseline_seq)` and returns `Held`
  when it is below the committed boundary:
  `server/src/messages.rs:872-912` (reject-ahead branches at 828-871).
- The intro nudge producer exists: `enqueue_channel_intro_nudge` at
  `src/puffo_agent/agent/membership_actions.py:178-235` emits
  `intro-prompt-<channel_id>`. Its wired baseline source reads only the
  unwritten store table (`BaselineAdapter` at
  `src/puffo_agent/agent/global_inbox_types.py:221-228` → `get_context_baseline`).
- Response validation echoes the numeric baseline back from the Server
  (sent and held shapes) at
  `src/puffo_agent/agent/send_response_validation.py:138-150,158-186`, so the
  `None` distinction is equally absent from the response side.

### User-visible impact

Only the first newly added Agent introduces itself immediately. Later new
Agents' introduction sends are held against a channel message they cannot read
until another visible channel message arrives and unblocks recovery (staging
timeline in F001).

### Confirmed evidence

- Current-source citations above (Agent + Server).
- History: integration commit `0d9f44d` introduced the `None -> 0` fallback in
  the audited lineage. Parallel historical branch commit `2e50c31` contains the
  same fallback and its focused test, but is not an ancestor of the audited
  HEAD and is supporting evidence only.
- Staging runtime log and per-Agent stores (attributed to F001; not re-run here).

### Unknowns

- The Server's omitted-baseline branch is not implemented in current source.
  The frozen contract assigns lazy establishment to the Server send
  transaction, not to membership or attach processing.
- With encrypted history, `null` plus `seen_seq == 0` cannot distinguish an old
  inaccessible channel head from a newly arrived visible message that has not
  reached the local store yet. That bounded first-send race is an explicit
  residual of the privacy-preserving design; later sends use the established
  numeric baseline and normal freshness coordination.
- Visibility scoping for baseline establishment (identity vs device vs locally
  decryptable deliveries).
- Two observations attached to F001 await independent classification:
  (1) an intro Inbox row can be marked `processed` even when the required
  outbound introduction was not committed; (2) a delayed recovery response can
  expose coordination internals (held send / unavailable synchronization
  boundary) to channel users.

### Repository owner

Agent (`send_coordinator`, `inbox_store`, `membership_actions`,
`global_inbox_types`) **and** Server (`agent_runtime_messages`, `messages`).

### Bounded acceptance criteria

1. `None`, `0`, and positive baseline values are preserved through local state,
   the Agent request, Server decision/response, and retry/replay; `None` is
   never coerced to `0`.
2. The Server lazily establishes a usable boundary without pretending the Agent
   saw pre-membership ciphertext.
3. Multiple Agents joining a channel with existing history each introduce
   themselves once without a follow-up human message.
4. A genuinely newer visible message still participates in freshness
   coordination (can hold a stale draft).
5. Restarting the daemon does not duplicate or abandon an outstanding intro.

---

## F002 — Agent busy status and chat activity strip are missing

**Classification: `restored`**

Resolution: Agent merge `8472281` restores the Global Inbox status lifecycle;
commit `8641a89` classifies membership, intro, and reminder envelopes as local
synthetic work so they update Agent busy state without inventing a processing
run for a nonexistent server message.

### Old contract

The pre-integration Worker wired the Server status lifecycle into the message
turn: `reporter.begin_turn(first_post_id)` before the batch and
`reporter.end_turn_batch(runs)` in `finally`
(`0d9f44d^:src/puffo_agent/portal/worker.py:1367,1434`). The Server stores
`agent_status` (`idle`/`busy`/`error` + `current_message_id`) and
`message_processing_runs`, which the chat UI renders as avatar busy state and
the activity strip.

### Pre-repair source behavior (frozen diagnosis)

- The Global Inbox turn path never calls the lifecycle:
  `src/puffo_agent/portal/worker_run.py:430-452` (`_execute_global_turn`) calls
  `context.puffo.handle_global_inbox_turn(planned)` with no
  `StatusReporter` involvement; `worker_run.py:588-625` builds the reporter and
  starts only its heartbeat loop.
- The only production callers of `begin_turn`/`end_turn_batch` are the WS-local
  session path (`src/puffo_agent/portal/ws_local/session.py:276,296,324-325`),
  not the Global Inbox path.
- The reporter's own contract still says the message handler must call the
  lifecycle: `src/puffo_agent/agent/status_reporter.py:32-37`.
- The legacy control-plane `agent.status` frame still fires from
  `src/puffo_agent/agent/core.py:380-404` (`turn_start`/`turn_complete`) via
  `src/puffo_agent/portal/control/reporter.py` (encrypted control WS), but that
  is a separate channel from the Server-backed status/run contract.

### User-visible impact

During real Global Inbox turns, Agent avatars do not show busy state and the
chat activity strip does not describe who is working on what. Web renders these
surfaces from Server-backed `agent_status_update` /
`message_processing_run_update` events, which the Agent never emits because no
processing run is opened (Web claim attributed to F002; not re-inspected).

### Confirmed evidence

- Current citations above (Agent).
- Server status/run contract: `server/src/agent_status.rs:63-68`
  (status + `current_message_id`), `server/src/agent_status.rs:370-409`
  (busy+run upsert), `server/src/agent_status.rs:538` (`get_message_processing_runs`).
- History: pre-integration `worker.py:1367,1434` wiring (verified with
  `git show 0d9f44d^:src/puffo_agent/portal/worker.py`).

### Unknowns

- Exact commit deployed by staging Web was not verified; a separate deployed-Web
  regression has not been ruled out (attributed to F002).

### Repository owner

Agent (status lifecycle wiring in the Global Inbox composition root); Server
owns the status/run storage and broadcast contract.

### Bounded acceptance criteria

1. A normal Global Inbox turn produces a busy status with the source message ID
   and one active processing run.
2. Avatar and activity strip appear during the turn and settle on success,
   failure, cancellation, and daemon shutdown.
3. Retry and held-reconsideration turns do not leave orphaned or duplicate
   active runs.
4. Synthetic local notices may show the Agent busy but must not invent a
   processing run for a nonexistent message.
5. Legacy control-plane activity and the chat integration status path have
   explicit, non-conflicting ownership.

---

## F003 — `cli-docker + codex` support regressed in 2.0.0a1

**Classification: `restored`**

Resolution: Agent merge `8fc5c43` restores the Docker Codex admission,
preparation, Driver transport, isolated runtime home, cleanup, and persisted
session compatibility paths. The real-provider-turn residual is recorded in
the status table rather than hidden by the passing bounded smoke.

### Old contract

Before integration, the runtime matrix accepted the `cli-docker` runtime with
provider `openai` and harness `codex`:
- `HARNESSES_FOR_RUNTIME[cli-docker] = {claude-code, codex}` and
  `DEFAULT_DOCKER_HARNESS_FOR_PROVIDER[openai] = codex`
  (`0d9f44d^:src/puffo_agent/portal/runtime_matrix.py`).
- `DockerCLIAdapter` ran a Codex app-server inside the per-agent container with
  isolated home, sanitized credentials, MCP config, and skills preparation.
- The bundled Docker image baked `@openai/codex@__CODEX_VERSION__`
  (`0d9f44d^:src/puffo_agent/agent/adapters/docker_cli.py:106-108`).
- `tests/test_docker_codex.py` covered the image dependency, isolated home,
  credential view, MCP config, app-server command, persisted sessions, and
  Docker binary selection.

### Pre-repair source behavior (frozen diagnosis)

- The runtime matrix rejects the combination:
  `src/puffo_agent/portal/runtime_matrix.py:204-211` admits only `claude-code`
  on `cli-docker` and tells the operator to set `runtime.kind` to `cli-local`.
- `DockerCLIAdapter` is Claude Code-only: it rejects any non-`claude-code`
  harness (`src/puffo_agent/agent/adapters/docker_cli.py:183-189`) and the
  Dockerfile installs only `@anthropic-ai/claude-code`
  (`src/puffo_agent/agent/adapters/docker_cli.py:103`).
- Integration commit `0d9f44d` deleted `src/puffo_agent/agent/adapters/codex_session.py`
  (1452 lines) and `tests/test_codex_session.py`, `tests/test_codex_session_audit.py`,
  `tests/test_docker_codex.py`; the commit message states "validate_triple now
  admits only claude-code on cli-docker".

### User-visible impact

An Agent persisted with `cli-docker + openai + codex` no longer starts after
upgrade. The validation error is explicit, but silently changing to `cli-local`
changes the execution and isolation model and is not an equivalent migration.

### Confirmed evidence

- Current citations above.
- History: `git show 0d9f44d --name-status` (deleted `codex_session.py` +
  three test files) and `git show 0d9f44d^:.../runtime_matrix.py` /
  `.../docker_cli.py` (pre-integration acceptance + image contents).

### Unknowns

- Han's complete Docker/Codex workload has not been reproduced on his host.
  This is not needed to prove the admission regression, but is required before a
  restored implementation can be claimed compatible with the new Global Inbox
  and runtime-event contracts.

### Repository owner

Agent.

### Bounded acceptance criteria

1. An Agent persisted as `cli-docker + openai + codex` loads without manual
   configuration edits.
2. Codex app-server runs inside the Agent's container with isolated, sanitized
   credentials and container-reachable MCP configuration.
3. The runtime satisfies the same Inbox admission, send, reminder, status, and
   runtime-event contracts as `cli-local + codex`.
4. Upgrade from an existing Docker Codex Agent preserves its workspace and
   documented session state.
5. `start`, `stop`, restart, auth failure, and container cleanup are exercised
   on at least one real Docker host.

---

## F004 — `puffo-agent stop` does not exit normally after upgrade

**Classification: `restored`**

Resolution: Agent merge `f65e9ee` restores legacy sentinel parsing and exact
PID-scoped shutdown behavior; follow-up commit `0609961` records the accepted
upgrade contract and evidence.

### Old contract

The pre-integration `.stop_requested` sentinel was a timestamp-only file, and
the daemon treated the mere existence of the file as a stop request
(`0d9f44d^:src/puffo_agent/portal/state.py:1502-1506`,
`write_stop_request` writes `str(int(time.time()))`).

### Pre-repair source behavior (frozen diagnosis)

- The sentinel is now PID-scoped JSON: `write_stop_request` writes
  `{"pid": ..., "created_at": ...}` (`src/puffo_agent/portal/state.py:1145-1156`;
  CLI writes it at `src/puffo_agent/portal/cli.py:344`).
- The daemon accepts only a JSON payload whose PID matches the running daemon:
  `read_stop_request_pid` (`src/puffo_agent/portal/state.py:1128-1138`) parses
  only JSON and explicitly treats timestamp-only sentinels as stale (returns
  `None`); `stop_requested_for(pid)` requires an exact PID match
  (`src/puffo_agent/portal/state.py:1141-1142`).

### User-visible impact

A stop request written by an older CLI is rejected by the new daemon, so
`puffo-agent stop` can fail to shut the new daemon down in a mixed-version
upgrade. Compatibility matrix of the wire formats:
- `2.0.0a1` CLI → `0.12.2` daemon: compatible (old daemon checks existence).
- `0.12.2` CLI → `2.0.0a1` daemon: incompatible (new daemon rejects the
  timestamp payload).
- `2.0.0a1` CLI → `2.0.0a1` daemon (same home): compatible.

### Confirmed evidence

- Current `state.py:1128-1156` and `cli.py:344`.
- History: `git show 0d9f44d^:src/puffo_agent/portal/state.py` (timestamp-only
  writer), `git log -S 'read_stop_request_pid'` shows the JSON format was
  introduced by `0d9f44d`.
- Finding's isolated check: `legacy_timestamp_accepted=False`,
  `pid_json_accepted=True` (attributed to F004; executable-level, not re-run).

### Unknowns

- Han's exact executable/daemon/`PUFFO_AGENT_HOME` pairing on the reported host
  is unverified; this cross-version cause is confirmed but does not alone prove
  his full report.
- Secondary shutdown-latency risk (new CLI waits 60s; daemon checks the sentinel
  only between reconcile iterations, and one reconcile can wait up to 120s for a
  Worker warm) is a confirmed architectural possibility but is not tied to the
  report.

### Repository owner

Agent.

### Bounded acceptance criteria

1. The supported upgrade path can stop a daemon started by the previous release,
   regardless of which supported CLI version writes the request.
2. CLI and daemon identify version, executable, PID, and Agent home in
   diagnostics without exposing credentials.
3. A stop request interrupts or observes long Worker warm operations instead of
   waiting for a full warm timeout before shutdown begins.
4. Docker and provider subprocess cleanup remain bounded and report which phase
   delayed exit.
5. The command distinguishes "request not recognized", "shutdown in progress",
   and "cleanup timed out".

---

## F005 — Agent Profile Log loses Driver detail and Codex telemetry

**Classification: `restored`**

Resolution: Agent merge `5bec939` restores safe Driver-to-legacy projection and
token/context telemetry; follow-up commit `d5ddb2b` records the compatibility
evidence. Raw provider frames and private chain-of-thought remain excluded.

### Old contract

The Web Profile Log consumed decrypted legacy `agent.status` events
(`turn_start`, `assistant_text`, `tool_use`, `turn_complete`) and rendered
`Thinking`/`Using`/`Sending` rows and `turn_complete.payload.tokens`. The old
`codex_session.py` processed the `last` and `total` sections of
`thread/tokenUsage/updated`, calculated per-turn input/output deltas, and
retained current-context usage; the old `core.py` added `current_context` to
`turn_complete` when present.

### Pre-repair source behavior (frozen diagnosis)

- The 2.0 core emits only the outer legacy boundaries and drops `current_context`:
  `src/puffo_agent/agent/core.py:380-404` emits `turn_start` and
  `turn_complete` (with `tokens` only). The pre-integration
  `turn_complete_payload` additionally included `current_context`
  (`0d9f44d^:src/puffo_agent/agent/core.py:325-330`).
- The Codex Driver maps only cumulative context totals and drops per-turn token
  deltas: `src/puffo_agent/agent/harness/drivers/codex.py:643-659`
  (`thread/tokenUsage/updated` → `ContextStatus` with `totalTokens` +
  `modelContextWindow`); its terminal event carries only `outcome`
  (`codex_driver.py:827-831`).
- `src/puffo_agent/agent/harness/runtime/runtime_manager.py:800-812` copies token fields
  onto `TurnResult` only if the terminal event contains them, otherwise both
  default to `0` — hence Codex `0/0`.
- Claude Code still supplies terminal tokens:
  `src/puffo_agent/agent/harness/drivers/claude_code.py:618-627`
  (`input_tokens`/`output_tokens` from `usage`).
- The old Claude session path still projects rich events into the legacy
  reporter (`src/puffo_agent/agent/adapters/cli_session.py:1351-1372`,
  `assistant_text`/`tool_use`), which explains the execution-path difference.
- Normalized Driver events are not projected into the legacy `agent.status`
  reporter, and the Runtime Event projector publishes a metadata-only
  vocabulary (no raw frames/chain-of-thought); the Web does not consume Runtime
  Events for the Profile Log (Web claim attributed to F005).

### User-visible impact

Agent Profile Log shows coarse `Working`/`Done` rows, Codex completion rows
render `in token: 0, out token: 0`, and context telemetry is absent.

### Confirmed evidence

- Current citations above (Agent).
- History: `git show 0d9f44d^:src/puffo_agent/agent/adapters/codex_session.py`
  (`tokenUsage` `last`/`total` delta math at lines ~1182-1215) and
  pre-integration `core.py` `current_context` handling.
- Current Web `bc5db876` consumes decrypted machine messages in
  `use-realtime.ts`, stores safe rows in `agent-status-history.ts`, and renders
  them through `AgentStatusHistory.tsx`.
- A paired real Codex turn rendered `Working`, `read_inbox`, `send_message`,
  bounded assistant summaries, terminal `Done`, and `14405/599` token usage.

### Residuals

- The Profile Log is a per-device live history. Events emitted while the
  device is disconnected are intentionally best-effort and are not replayed as
  a durable cross-device audit log.
- Future presentation changes must not be conflated with publishing raw
  chain-of-thought, tool arguments, or provider-native payloads.

### Repository owner

Agent (driver telemetry + legacy projection); Web owns the consuming Profile
Log contract, inspected and exercised at `bc5db876`.

### Bounded acceptance criteria

1. Codex and Claude Code both preserve terminal token telemetry when supplied
   by their native protocols.
2. Context usage reaches the Profile surface through one documented contract.
3. A Driver turn exposes safe lifecycle/tool progress without publishing
   private reasoning or provider-native payloads.
4. The Web consumes the chosen authoritative stream; compatibility behavior is
   explicit while the legacy path remains supported.
5. Profile Log, avatar status, and chat activity strip agree on turn start,
   terminal state, failure, cancellation, and reconnect.

---

## F006 — Agent Profile Activity and Files are incomplete projections

**Classification: `intentionally deferred`**

### Old contract

No complete pre-2.0 contract existed for these surfaces. Git history shows the
Activity cache view and the Files `Coming soon` placeholder predate the Agent
Foundation integration; Agent 2.0 exposed their incompleteness during release
testing rather than removing an established pipeline.

### Current source behavior

Agent source does not feed these surfaces. Current Web inspection confirms
Activity is a view over the current Web message cache and Files renders the
static `No files yet · Coming soon` placeholder. Pairing a current Python
daemon enables the tab but does not create a file index.

### User-visible impact

Profile Activity is incomplete and changes with the loaded browser history;
Files never lists shared files or images.

### Classification rationale

Intentionally deferred: F006 predates 2.0, has no complete legacy interface for
the Agent to restore, and is product backlog per the frozen goal ("F006
Activity/Files is product backlog, not compatibility work").

### Repository owner

Web / product (not Agent).

### Bounded acceptance criteria

None defined in this workstream (product design decisions pending). Candidate
acceptance shape is recorded in F006: durable paginated Activity source with
explicit scope; Files at minimum listing accessible attachments the Agent
shared, with originating conversation/message; private local workspace files
never exposed merely because a viewer can open a Profile; empty states
distinguish `no results`, `not loaded`, `not supported`, `not authorized`.

---

## Additional evidence-backed pre-2.0 Web-visible contracts

Within the bounded evidence inventory, F001-F005 are restored and F006 remains
intentionally deferred. Acceptance found the additional stale Runtime-session
projection failure documented above; it is fixed at Server `ae0f238`.

The legacy Python `machine link` command and current Web `/link-machine` route
were exercised together successfully. The earlier claim that current Web only
exposed `/agents/pair` came from a Web worktree 211 commits behind
`origin/main`; it is superseded and must not motivate restoration of the
deprecated local HTTP bridge.

The extended behavioral acceptance prompt asking five Agents to count five
times stopped after `11/25`. Its Inbox slice was complete and untruncated, and
the deciding Agent returned `[SILENT]` despite an open repeated obligation.
Ordinary counting (`1,2,3,4,5`) and the fourth-is-one variant (`1,2,3,1,5`)
passed. This remains a model-behavior residual rather than evidence that the
baseline, status, or Runtime Events compatibility contracts are broken.

---

## Historical implementation slices (completed)

These were the ownership bounds used while restoring the contracts. They are
retained as implementation history. Normalized Harness Driver events remain the
single internal source of truth.

### Slice 1 — Agent-only legacy status/processing and safe projection (observability)

- Project normalized Driver events into the legacy encrypted `agent.status`
  stream for the Profile Log: safe `assistant_text`/`tool_use` activity and
  terminal `turn_complete` with token counts.
- Codex per-turn input/output token telemetry must be restored from
  `thread/tokenUsage/updated` (the `last`/`total` delta pattern that
  `codex_session.py` implemented pre-integration) and surfaced through
  `TurnResult` into `turn_complete`.
- Claude Code terminal `input_tokens`/`output_tokens` (already present in
  `claude_code_driver.py:618-627`) must be preserved.
- Context usage reaches the Profile surface through one documented contract.
- No raw chain-of-thought, native frames, or provider-native payloads may be
  projected (metadata-only vocabulary is retained).
- Implicated files (Agent): `src/puffo_agent/agent/harness/drivers/codex.py`,
  `src/puffo_agent/agent/harness/drivers/claude_code.py`,
  `src/puffo_agent/agent/harness/runtime/runtime_manager.py`,
  `src/puffo_agent/agent/harness/runtime/local_runtime.py`,
  `src/puffo_agent/agent/core.py` (legacy `turn_complete` emission),
  `src/puffo_agent/agent/adapters/cli_session.py` (existing projection pattern),
  `src/puffo_agent/portal/control/reporter.py`.

### Slice 2 — Coordinated Agent/Server optional-baseline wire contract

- Preserve `None` (no authoritative baseline), `0` (authoritative empty
  baseline), and positive values (history through that sequence is
  pre-context) through every hop:
  - local state: `src/puffo_agent/agent/inbox_store.py` (`channel_context_state`);
  - Agent request: `src/puffo_agent/agent/send_coordinator.py:869-871,669-679`
    (remove the `None -> 0` coercion);
  - Agent response validation: `src/puffo_agent/agent/send_response_validation.py`
    (sent/held freshness echo must preserve the distinction);
  - Server DTO/validation: `server/src/v2/agent_runtime_messages.rs:27-31,73-96`
    (make `context_baseline_seq` optional);
  - Server decision: `server/src/messages.rs:828-912` (define the `None` branch
    instead of treating it as `0`).
- Lazy establishment: the Server establishes a usable boundary for a new member
  without pretending the Agent saw pre-membership ciphertext.
- Replay/retry keep the same baseline meaning across retries.
- Multiple Agents joining an existing-history channel each introduce themselves
  once without a follow-up human message; genuinely newer visible messages still
  participate in freshness coordination.
- After a successful first coordinated send, persist the Server-established
  numeric baseline through `BaselineAdapter` so later sends and daemon restarts
  use normal freshness coordination. Membership/attach processing emits the
  intro notice only and does not inspect or manufacture an encrypted-history
  boundary.
- Implicated files: Agent `send_coordinator.py`, `inbox_store.py`,
  `global_inbox_types.py`, `send_response_validation.py`;
  Server `agent_runtime_messages.rs`, `messages.rs`.

### Overlapping state owners, wire files, and the coordination rule

The slices feed the same composition root but have separable ownership when the
file bounds below are enforced:

- Slice 1 owns `portal/worker_run.py`, Harness Driver/runtime files,
  `harness/local_runtime.py`, and `agent/core.py`.
- Slice 2 owns `send_coordinator.py`, `send_response_validation.py`,
  `global_inbox_types.py`, `inbox_store.py`, and the bounded Server freshness
  files.
- Neither slice should restructure `global_inbox_runtime.py`. If source
  inspection proves that file unavoidable, serialize only that integration
  edit after the independent implementations are complete.
- F002 status-lifecycle wiring is part of Slice 1 and must not be implemented in
  a third branch.

**Coordination rule:** Server baseline work and Agent observability may proceed
in parallel because their files do not overlap. Agent baseline work follows the
frozen Server wire contract and may also proceed independently of observability
while the ownership bounds above are respected. Serialize only an unavoidable
shared composition-root edit. This keeps normalized Driver events authoritative
without blocking unrelated compatibility restoration.

---

## Source evidence index

Pre-repair Agent diagnosis (at
`dd5f4ef517decaa814ffb9bc27b7c2d2b53efc89`):

- `src/puffo_agent/portal/runtime_matrix.py:204-211` (F003 current rejection)
- `src/puffo_agent/agent/adapters/docker_cli.py:103,183-189` (F003 Claude-only)
- `src/puffo_agent/portal/state.py:1128-1156` (F004 JSON sentinel)
- `src/puffo_agent/portal/cli.py:344` (F004 CLI write)
- `src/puffo_agent/portal/worker_run.py:430-452,588-625` (F002 no lifecycle)
- `src/puffo_agent/portal/ws_local/session.py:276,324-325` (F002 sole callers)
- `src/puffo_agent/agent/status_reporter.py:32-37` (F002 lifecycle contract)
- `src/puffo_agent/agent/core.py:380-404` (F002/F005 legacy emission)
- `src/puffo_agent/agent/inbox_store.py:1024-1061` (F001 storage)
- `src/puffo_agent/agent/send_coordinator.py:669-679,869-871` (F001 coercion + request)
- `src/puffo_agent/agent/send_response_validation.py:138-186` (F001 response echo)
- `src/puffo_agent/agent/membership_actions.py:178-235` (F001 intro nudge)
- `src/puffo_agent/agent/global_inbox_types.py:221-228` (F001 baseline adapter)
- `src/puffo_agent/agent/harness/drivers/codex.py:643-659,827-831` (F005)
- `src/puffo_agent/agent/harness/drivers/claude_code.py:618-627` (F005)
- `src/puffo_agent/agent/harness/runtime/runtime_manager.py:800-812` (F005)
- `src/puffo_agent/agent/adapters/cli_session.py:1351-1372` (F005 projection)

Pre-repair Server diagnosis (at
`393a1af6f54b9f44e0711437bd787ce0f093e80b`):

- `server/src/v2/agent_runtime_messages.rs:27-31,73-96` (F001 DTO/validation)
- `server/src/messages.rs:828-912` (F001 send decision / held)
- `server/src/agent_status.rs:63-68,370-409,538` (F002 status/run contract)

History (`git show <commit>:<path>` verified for each):

- `0d9f44d^:src/puffo_agent/portal/worker.py:1367,1434` (F002 old lifecycle)
- `0d9f44d^:src/puffo_agent/portal/runtime_matrix.py` (F003 old matrix)
- `0d9f44d^:src/puffo_agent/agent/adapters/docker_cli.py:106-108` (F003 old image)
- `0d9f44d` name-status (F003 deleted `codex_session.py` + test files; F004
  `-S 'read_stop_request_pid'`)
- `0d9f44d^:src/puffo_agent/portal/state.py:1502-1506` (F004 old sentinel)
- `0d9f44d^:src/puffo_agent/agent/adapters/codex_session.py:1182-1215` (F005 old tokens)
- `0d9f44d^:src/puffo_agent/agent/core.py:325-330` (F005 old `current_context`)
- `0d9f44d` blame/history for the current-lineage F001 `None -> 0` fallback;
  parallel non-ancestor `2e50c31` is supporting evidence only

## Confirmed evidence vs unknowns

Each entry separates confirmed source/history evidence from explicitly stated
unknowns. The resolution table adds focused tests and local product acceptance,
including a paired-device Profile Log UI test. It still does not claim a live
Docker provider turn where that environment was unavailable.
