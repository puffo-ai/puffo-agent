# Agent Foundation 2.0 Stable-Readiness Source Audit

Reviewed, source-grounded inventory for Agent Foundation 2.0 stable readiness.
This is a documentation audit, not an implementation pass.

## Post-audit implementation status (2026-08-11)

This section records work completed after the frozen audit base. It does not
rewrite the source evidence or classifications below.

- **A1 local implementation complete:** Agent merge commit `8a56124` routes
  the Claude Driver executable through `normalize_launch_argv`, including npm
  `.cmd`/`.bat`, PowerShell `.ps1`, direct `.exe`, and `%APPDATA%\npm`
  discovery. Focused construction tests pass. One real Windows Claude turn is
  still external and `not locally verified`.
- **K1 local implementation complete:** Agent merge commit `ea83bc2` provides
  resumable keyless foreign-DM and invitation operator authorization, bridge
  polling/control routing, fail-closed persistence, and durable ACK ordering.
  The companion Server feature branch is at `393a1af` and provides self-scoped
  invitation listing and serialized decisions. Agent focused checks and the
  full Python suite pass; a real Agent-to-Server bridge scenario remains
  external and `not locally verified`.

## Method and provenance

- **Frozen source base (immutable):** `b3669377d0978f3b4cdf02647f170522a32ed05e`
  ("docs: organize Agent Foundation 2.0 readiness", 2026-08-11). The worktree
  `HEAD` equals this SHA and was clean at audit start (`git status --short`
  empty). All path/symbol evidence below was read from this commit unless a
  different immutable commit is named.
- **Newer scope authority:** the current workstream goal at
  `loop-engineering/runtime/agent-foundation-2.0/goal.md` is treated as newer
  authority than the older backlog wording in
  `roadmap/agent-foundation/STABLE-2.0-READINESS.md` and
  `docs/RELEASE-CANDIDATE-2.0.0a1.md`. The goal's completion conditions and
  user-consensus item 6 bring keyless invitation/DM **operator authorization**
  into this workstream (see [Authority reconciliation](#authority-reconciliation)).
  The goal file itself is **external to the git worktree** (it lives in the
  main checkout under `loop-engineering/`, which is not a tracked repository
  path), so it is quoted as authority but is not a worktree-resolvable local
  path.
- **Vocabulary (closed):** every inventory row has exactly one primary
  classification: `confirmed local Agent gap`, `external dependency/evidence
  gate`, `already satisfied`, `stale/inaccurate wording`, or `not a blocker for
  this branch`. Where an item spans local and external concerns, the
  classification answers whether **this branch** has a confirmed
  code/documentation obligation; the other concern is described in the row's
  "local action / external evidence" field.
- **Evidence discipline:** remote PR state, production inventory, cloud
  promotion, staging scenarios, package publishing, and real-Windows execution
  that are not provable from this repository are explicitly marked
  `not locally verified`. The old backlog's assertion is never cited as proof
  of itself, and mutable branch names are only used alongside immutable commit
  facts.

## Canonical inventory

| ID | Backlog item | Primary classification | Local action / external evidence |
|---|---|---|---|
| A1 | New Claude Driver launches the resolved executable directly; no Windows npm-shim handling | `confirmed local Agent gap` | Driver-boundary command normalization + focused construction tests are local work; one real-Windows Claude turn remains `not locally verified`. |
| S1 | Metadata-only Runtime Event contract not yet deployed with the Agent candidate (Server PR #273 open) | `external dependency/evidence gate` | Agent-side vocabulary/outbox/uploader is committed and locally satisfied; Server PR merge/deploy + staging are `not locally verified`. |
| O1 | Production `agent_status.runtime` not inventoried for active Docker Codex / retired direct-runtime configs | `external dependency/evidence gate` | Status-reporting code that feeds `agent_status.runtime` is local; querying/mutating production inventory is `not locally verified`. |
| C1 | AIM/E2B templates not pinned and promoted from an immutable Agent commit | `external dependency/evidence gate` | Cloud harness code exists locally; no template pin/promotion machinery or records exist in this repo and none may be invented here. |
| V1 | No consolidated staging evidence record against one immutable candidate SHA | `external dependency/evidence gate` | Release-contract acceptance set is documented locally; staging execution evidence is `not locally verified`. |
| R1 | Stable package metadata and publishing remain intentionally disabled | `not a blocker for this branch` | Local metadata is already at `2.0.0a1`; publishing is external and forbidden in this run. |
| K1 | Newer requirement: keyless invitation/DM operator authorization | `confirmed local Agent gap` | Identity/operator, invitation, DM-gate, and keyless-transport contracts exist; the operator-request/decision route for the keyless transport is missing (see below). |

## A1 — Windows Claude Driver command launch

### Current Driver boundary (frozen base)

- `src/puffo_agent/agent/harness/claude_code_driver.py::ClaudeCodeCliDriver.open`
  (line 103) builds the launch argv at lines 110–120:

  ```python
  args = [
      spec.executable or "claude",
      *spec.launch_args,
      "-p", "--input-format", "stream-json", "--output-format",
      "stream-json", "--replay-user-messages", "--verbose",
  ]
  ```

  and launches it **directly** via `asyncio.create_subprocess_exec(*args, ...)`
  at `claude_code_driver.py:137`. There is no platform-normalization step
  between `spec.executable` and the subprocess argv. A repository-wide search
  for `win32`/`windows`/`.cmd`/`powershell`/`shim` in
  `src/puffo_agent/agent/harness/` and `tests/test_harness_driver.py` returns
  nothing.
- `RuntimeSpec.executable` (`src/puffo_agent/agent/harness/driver.py:102`)
  carries the resolved binary path. It is populated by
  `LocalRuntime._prepare_claude_spec` (`src/puffo_agent/agent/harness/local_runtime.py:402`)
  from `resolve_claude_bin()` (`cli_bin.py:59`) and stored at
  `local_runtime.py:476`.
- `src/puffo_agent/agent/cli_bin.py` at the frozen base exposes
  `resolve_claude_bin` (line 59) but **not** `spawn_argv` / `windows_runnable`
  (`git show b3669377:src/puffo_agent/agent/cli_bin.py` has no such symbols).
- Current focused tests: `tests/test_harness_driver.py` drives
  `ClaudeCodeCliDriver` through a `process_factory` double; the only argv
  assertion is `assert "--replay-user-messages" in captured_args`
  (`test_harness_driver.py:1343`). `tests/test_local_runtime_migration.py:91`
  asserts `prepared.spec.executable == "/opt/bin/claude"`. No test pins the
  executable head or any Windows shim behavior for the Driver.

### Immutable git evidence (Windows shim work)

- `1f64930ef1a4bc5ff09f415dec072f4d93a708fb` ("PUF-420 (agent): spawn claude
  by resolved path, and wrap Windows shims") and
  `9ffae53567fdd827c9087a52f26477a4296eb469` ("PUF-420: fall back to the bare
  name instead of raising in _build_command") are **not ancestors** of the
  frozen base (`git merge-base --is-ancestor` fails for both). They exist on
  the remote branch `origin/puf-420-windows-claude-spawn` (branch tip
  `9ffae53`). The base therefore does **not** contain the fix.
- On that branch, commit `1f64930` adds `windows_runnable()` and `spawn_argv()`
  to `src/puffo_agent/agent/cli_bin.py` (`.cmd`/`.bat` → `cmd.exe /c`,
  `.ps1` → `powershell -NoProfile -NonInteractive -File`, `.exe` passes
  through) and uses them in the **old** `adapters/local_cli.py::_build_command`
  via `cmd = spawn_argv(claude_bin) if claude_bin else ["claude"]`
  (`local_cli.py:995` on the branch). The old adapter file
  `adapters/local_cli.py` does not exist at the frozen base (the path moved to
  `agent/harness/`), so the shim helpers must be forward-ported to the **new**
  Driver boundary, not copied onto a dead adapter.
- `9ffae53` adds `tests/test_windows_claude_spawn.py` (17 argv-construction
  tests) and pins the resolver-miss fallback to the bare name. The commit
  message states the evidence limit verbatim: *"sys.platform is monkeypatched
  since there is no Windows CI: these pin argv construction, NOT that
  CreateProcess accepts the result. A real Windows run is still the only proof
  of the fix."*

### Smallest confirmed change

Normalize `RuntimeSpec.executable` at the Driver command-launch boundary before
the `asyncio.create_subprocess_exec(*args)` call — i.e. route the resolved
executable through a Windows-runnable/spawn-argv normalization (the behavior
`windows_runnable` + `spawn_argv` provide on the PUF-420 branch) so `.cmd` /
`.bat` / `.ps1` shims are launched through their interpreter, `.exe` passes
through, and POSIX is unchanged — plus focused command-construction tests.
This is the smallest change that restores the confirmed shim behavior at the
new boundary; no broader refactor is prescribed.

### Real-Windows evidence limitation

Mocked/platform-monkeypatched argv tests (the PUF-420 pattern) pin argv
construction only. They cannot replace one real Windows Claude turn. The
frozen base has no Windows CI, and this repo cannot produce real-Windows
execution evidence, so real-Windows acceptance remains `not locally verified`
and external.

## K1 — Keyless invitation / DM operator authorization

The newer goal requires: when an invitation or DM action is not already
operator-authorized, the Agent should route a clear authorization request to
its configured operator rather than silently accepting, silently rejecting, or
treating an external party as the operator. The trace below follows the
identity/operator, invitation, DM-gate, and keyless-transport contracts.

### Identity / operator contracts

- `operator_slug` is configured at provision/portal time and threaded into the
  client: `src/puffo_agent/agent/client_setup.py` (fields at lines 26, 111–129),
  `src/puffo_agent/portal/state.py:468`, and
  `src/puffo_agent/portal/control/provision.py:91–104`; consumed by
  `PuffoCoreMessageClient.__init__` (`src/puffo_agent/agent/puffo_core_client.py:157–180`).
- Operator root-key identity comes from the identity cert:
  `parse_operator_pubkey` (`src/puffo_agent/agent/client_support.py:33–48`)
  reads `declared_operator_public_key`; `PuffoCoreMessageClient.listen` caches
  `self._operator_root_pubkey` from it (`puffo_core_client.py:272–281`).
- `inviter_is_operator` (`src/puffo_agent/agent/membership_events.py:483–504`)
  accepts an inviter **only** if the inviter's slug equals `operator_slug` or
  the inviter's fetched root public key equals `operator_root_pubkey`. An
  external inviter is never promoted to operator. This invariant is preserved
  by any future work and is not changed by this audit.

### Native invitation flow (signed transport)

- `invite_poll_loop` (`membership_events.py:17–35`) drives
  `poll_pending_invites` (`membership_events.py:300–336`,
  `http.get("/invites?direction=received")`), which calls `process_invite`
  (`membership_events.py:347–420`). `process_invite` auto-accepts only when
  `inviter_is_operator` is true or the space auto-accept flag is set; otherwise
  it calls `notify_operator_of_invite`.
- `PuffoCoreMessageClient._notify_operator_of_invite`
  (`puffo_core_client.py:1714–1787`) DMs the configured operator a permission
  prompt and records the prompt in `_pending_invite_dms` keyed by envelope id.
  The operator's reply is consumed by `operator_control_gate`
  (`src/puffo_agent/agent/ingress_policy.py:120–177`) via
  `_apply_invite_replies` / `resolve_invite_targets`
  (`src/puffo_agent/agent/membership_actions.py:591–641`). Reply routing is
  pinned by `tests/test_invite_reply_routing.py`.

### Keyless skipped invite-poll boundary

- `listen_bridge` (`src/puffo_agent/agent/bridge_transport.py:62–104`)
  "Deliberately does NOT start `_invite_poll_loop` / `_warm_member_caches` —
  they drive signed HTTP endpoints that can't work keyless" (lines 73–76).
- `PuffoCoreMessageClient.listen` (`puffo_core_client.py:258–314`) returns
  through `_listen_bridge()` whenever `_bridge` is set (lines 268–270); the
  invite poll task is only created on the native WS branch (line 290). The
  bridge frame dispatch (`dispatch_bridge_frame`, `bridge_transport.py:107+`)
  handles `message`, `pending_delivered`, `runtime_command`, `added_to_space`,
  and `error` — no invite frames. Consequence: a keyless agent never learns of,
  and never asks the operator about, a pending invitation; `_pending_invite_dms`
  is never populated keyless.

### Shared foreign-DM gating

- `foreign_dm_gate` (`src/puffo_agent/agent/ingress_policy.py:180–253`) is the
  one Agent-owned DM approval gate used by both transports
  (`_bridge_gate_verdict`, `bridge_transport.py:485–523`, applies it at line 514).
  For the keyless transport (`signed_http_available` returns False for bridge
  clients, `ingress_policy.py:68–78`), a foreign DM from a non-trusted sender
  with an `operator_slug` configured is held with disposition
  `FOREIGN_DM_GATED` and logged as *"keyless transport cannot send the operator
  approval prompt … holding the DM gated rather than delivering it unapproved"*
  (`ingress_policy.py:221–241`). The prompt is **never sent** to the operator.
- Durable gated receipt behavior: `store_bridge_payload`
  (`bridge_transport.py:398–435`) commits the verdict via `_commit_bridge_verdict`
  (`bridge_transport.py:526–569`); `_redelivery_ack`
  (`bridge_transport.py:438–448`) returns `False` for `FOREIGN_DM_GATED` rows,
  so a held DM is never acked and stays queued server-side until the operator
  answers. Pinned by `tests/test_bridge_ingress_policy.py`:
  `test_foreign_dm_over_bridge_is_gated_not_eligible` (line 159),
  `test_legacy_seqless_bridge_frames_carry_verdict_into_storage` (line 178,
  which shows `store.promote_gated_receipt` can release such a row).
- Native release path exists but is keyless-inaccessible: `promote_gated_receipt`
  is called in product code only by the signed WS path
  `src/puffo_agent/agent/inbound_receipts.py:54`, and the approval reply
  handler `maybe_handle_dm_approval_reply` (`src/puffo_agent/agent/dm_gate.py:104–186`)
  requires `_pending_dm_approvals` (never populated keyless), signed
  `/allowlists` / `/blocklists` POSTs, and `client._ws` for draining
  (`dm_gate.py:189–228`). None of that exists on the keyless transport.

### Available keyless transport operations

- `CloudBridgeClient` (`src/puffo_agent/agent/bridge_client.py:71+`) exposes
  `send_send` (500), `send_fetch_pending` (550), `send_status` (558),
  `send_ack` (608), `send_list_spaces` (621), plus status/wake/blob operations.
- A keyless plaintext DM **can** be sent: `send_bridge_fallback_dm`
  (`src/puffo_agent/agent/outbound_messages.py:154–173`) uses
  `bridge.send_send(plaintext, recipient_slug, …)`. Today it is wired only to
  the fallback DM reply to `_last_dm_sender`
  (`puffo_core_client.py:1884–1899`) — not to any authorization request.
- An operator's DM over the bridge **is** intercepted: `operator_control_gate`
  keys on `payload.sender_slug == client.operator_slug`
  (`ingress_policy.py:135–139`); pinned by
  `tests/test_bridge_ingress_policy.py::test_operator_control_reply_over_bridge_is_consumed_not_delivered`
  (line 126).

### Missing behavior boundary

The exact missing boundary is the absent **keyless-capable operator-request /
decision route**: a keyless agent has (a) no invite poll, so it never requests
a decision about an unauthorized invitation; (b) no operator approval prompt
for a gated foreign DM, so it holds the DM without asking; and (c) no keyless
apply path (`_pending_dm_approvals` / signed allowlist-blocklist / `_ws` drain
are all native-only), so even an operator reply over the bridge has no pending
state or keyless side effect to act on. The piece the goal calls for is a route
that, on the keyless transport, sends a decision request to the configured
operator through an operation the bridge already supports (e.g. `send_send`)
and applies the operator's answer through the durable gated-receipt lane
(`promote_gated_receipt`/`tombstone_gated_dms_from`) plus the keyless local
contact state, while the server-side blocklist/allowlist writes remain
native-only. Two invariants hold for any such design: an external inviter or
sender is **never** promoted to operator (`membership_events.py:483–504`), and
no channel-encryption or plaintext policy is designed, duplicated, or
reclassified here (Han's scope, per
`docs/RELEASE-CANDIDATE-2.0.0a1.md` decisions and this audit's out-of-scope).

## S1 — Runtime Event contract

- Locally committed and satisfied on the Agent side: fixed-vocabulary schema
  `src/puffo_agent/agent/runtime_events.py` (`RUNTIME_EVENT_TYPES` lines 14–20,
  `validate_runtime_event` lines 96–149), allowlist-only projection
  (`RuntimeEventProjector`, lines 220–306), durable outbox and serial uploader
  (`RuntimeEventOutbox`, `RuntimeEventUploader` in
  `src/puffo_agent/agent/runtime_event_outbox.py`), wiring in
  `src/puffo_agent/portal/worker_run.py`. Vocabulary/privacy pinned by
  `tests/test_runtime_events.py` (e.g. `test_schema_has_exact_v1_envelope_and_metadata_only_types`,
  line 31) and `tests/test_runtime_event_outbox.py`.
- The backlog's S1 blocker is Server PR #273 merge/deploy status
  (`docs/RELEASE-CANDIDATE-2.0.0a1.md:40–42`). The Server is a separate
  repository; its PR state, deployment, and staging behavior are `not locally
  verified`. Staging behavior and "only fixed-vocabulary metadata reaches the
  Server" remain external evidence, not derivable from local unit tests.

## O1 — Production `agent_status.runtime` inventory

- Repository-local: the status reporter feeds the server's `agent_status`
  `runtime` field — `src/puffo_agent/agent/status_reporter.py::_runtime_payload`
  (line 116) allows only `kind/provider/harness/model/inference_level`; the
  keyless bridge `send_status` folds into the same `agent_status` row
  (`src/puffo_agent/agent/bridge_client.py:569`); `runtime_provider` is wired at
  `src/puffo_agent/portal/worker.py:1217`.
- The actual **production** inventory of active Docker-Codex and retired
  direct-runtime configurations requires querying production, which is
  `not locally verified` and out of scope for this run. No repository
  configuration or migration code proves what is active in production.

## C1 — AIM/E2B template pinning

- Repository-local: cloud harness support exists — E2B egress/token references
  in `src/puffo_agent/agent/bridge_client.py` (5, 43–44),
  `src/puffo_agent/crypto/http_client.py` (39, 231),
  `src/puffo_agent/crypto/http_session.py` (39), `src/puffo_agent/mcp/config.py`
  (390), `src/puffo_agent/mcp/puffo_core_tools.py` (196), and the historical
  note `roadmap/cloud-agent/FAT-E2B-INTEGRATION.md`.
- No immutable template pin, staging promotion record, or migration plan exists
  anywhere in this repository, and none may be invented here. Those remain
  external evidence gates (`not locally verified`).

## V1 — Consolidated candidate staging evidence

- The required acceptance set is documented in
  `docs/RELEASE-CANDIDATE-2.0.0a1.md:57–93` (install, upgrade of `1.2.0`
  state, one real Claude and one real Codex turn, three coordination cases, one
  held draft through context recovery, multi-target Inbox delivery, restart
  during pending work, staging Runtime-Event privacy inspection).
- No consolidated evidence record for these cases against one immutable
  candidate SHA is committed at the frozen base. `docs/integration-tests/python-agent-message-runtime-system-e2e-20260728.md`
  is an earlier integration record, not the candidate acceptance set. Local unit
  tests (e.g. `tests/test_global_inbox_runtime.py`,
  `tests/test_global_inbox_runtime_held_and_reminders.py`) exercise components
  but are **not** staging proof and are not presented as such.

## R1 — Stable metadata and publishing

- Local state at the frozen base already satisfies "intentionally disabled":
  `pyproject.toml:14` is `version = "2.0.0a1"`; `README.md:76–79` describes the
  TestPyPI-only candidate and links the release-candidate doc; `CHANGELOG.md`
  has a `[2.0.0a1]` entry dated 2026-08-11; the newest local tag is `v1.2.0`
  (no `v2.0.0` tag); `.github/workflows/publish-pypi.yml:48–66` rejects any
  non-stable version and requires a matching `vX.Y.Z` tag, so a stable publish
  is currently impossible and publishing is an external action in any case.
- R1's prerequisite ordering (A1 → S1 → O1 → C1 → V1 before R1) is a release
  policy, not a local code obligation. This branch has no confirmed
  code/documentation obligation for R1, hence `not a blocker for this branch`;
  stable publication remains `not locally verified` and forbidden in this run.

## Authority reconciliation

The older backlog (`roadmap/agent-foundation/STABLE-2.0-READINESS.md`,
"Explicitly Out Of Scope") and the release candidate
(`docs/RELEASE-CANDIDATE-2.0.0a1.md:52–55`) classify *"keyless invitation
approval, complete keyless DM trust management"* as separate product work. The
current goal (`loop-engineering/runtime/agent-foundation-2.0/goal.md`,
user-consensus item 6 and completion conditions) is newer authority and brings
a **bounded operator-authorization requirement** into this workstream: when an
invitation or DM action is not already operator-authorized, the Agent routes a
clear authorization request to its configured operator instead of silently
accepting, silently rejecting, or treating the external party as the operator.

This audit replaces the stale classification for that bounded requirement only.
It remains out of scope here (and for any implementation) that: the **broad**
keyless DM trust-management surface and Runtime Event UI stay separate product
work; and Han-owned channel encryption / plaintext-channel policy
(`docs/RELEASE-CANDIDATE-2.0.0a1.md:24–26`, decisions item 3) is neither
designed, duplicated, nor reclassified by this inventory or by the K1
recommendation. `roadmap/agent-foundation/STABLE-2.0-READINESS.md` is updated
in this run to link this audit and reflect this reconciliation; no release gate
is added or removed.

## Ordered recommendation

The source traces confirm both premises of the task, so the first two future
implementation units are, in order:

1. **Windows Claude Driver shim normalization** (A1): route
   `RuntimeSpec.executable` through the Windows shim/spawn normalization at the
   Driver command-launch boundary in
   `src/puffo_agent/agent/harness/claude_code_driver.py::open`
   (and the executable source in
   `src/puffo_agent/agent/harness/local_runtime.py` / `cli_bin.py`), with
   focused command-construction tests modeled on the PUF-420
   `tests/test_windows_claude_spawn.py` pattern. Real-Windows acceptance stays
   an external evidence gate.
2. **Keyless invitation/DM operator authorization** (K1): add the
   keyless-capable operator-request/decision route described under
   [Missing behavior boundary](#missing-behavior-boundary), grounded in the
   existing contracts — identity/operator (`client_support.py`,
   `membership_events.py::inviter_is_operator`), bridge ingress
   (`bridge_transport.py`, `ingress_policy.py::foreign_dm_gate`),
   membership/DM authorization (`membership_actions.py`, `dm_gate.py`,
   `message_store.py::promote_gated_receipt`), and the keyless transport
   (`bridge_client.py::send_send`, `outbound_messages.py::send_bridge_fallback_dm`).

Likely non-overlapping boundaries: Driver/CLI launch normalization (A1) versus
identity, bridge ingress, and membership/DM authorization (K1), each with its
own focused tests. No implementation, test design beyond the observed
boundaries, deployment step, or encryption/plaintext policy is specified here.

### External evidence work (out of scope for any execution in this run)

Keep separate from local code work; none of these actions occur in this run:

- **S1** — merge and deploy puffo-server PR #273; staging confirmation that only
  fixed-vocabulary metadata reaches the Server.
- **O1** — query production `agent_status.runtime`; record migration, pin, or
  retirement decisions for every active runtime class.
- **C1** — AIM/E2B template pinning and promotion from an immutable Agent
  commit; staging validation; existing-cloud-Agent migration plan.
- **V1** — run the release-contract acceptance cases against one immutable
  candidate SHA and record the evidence.
- **R1** — package build/publish/tag/release, and the version/README/changelog
  change to `2.0.0`, only after the applicable gates close.
