# FAT-CLOUD Architecture Doc — Rubric Review + Delta-from-Desktop Cross-Comparison

> **Subject.** `docs/FAT-CLOUD-ARCHITECTURE.md` (this worktree, branch
> `fleet/fatcloud-arch-review`, stacked on `fleet/fatcloud-arch-doc`).
> **Rubric.** `puffo-server roadmap/cloud-agent/ARCH-DOC-REVIEW-RUBRIC.md` (7 dims + 3 hard metrics).
> **Method.** Ground truth was **reconstructed from code first** (`src/puffo_agent/**`, the
> `fleet/fat-cloud-phase1` branch, the desktop arch doc `fleet/pyagent-arch-doc:docs/ARCHITECTURE.md`,
> and the current `puffo-server` server code), **before** the doc's claims were re-read as truth
> (anti-anchoring, per the rubric's core principle). Every finding cites `file:line`.
> This review is evidence-bound and adversarial: it reports what the code says, not an opinion of the prose.
>
> **Verdict: NEEDS-REVISION** — see [§7 Verdict](#7-verdict--severity-ranked-findings).
> Gating: two mis-stated `[DESIGNED]` statuses that are actually **[BUILT]** in `puffo-server` (M6),
> plus a load-bearing observability capability (`status_reporter.py`) both omitted and broken by the
> swap (M1/cross-comparison). The doc is otherwise **accurate (~0.90) and scrupulously honest in
> intent** — the revision is bounded and additive.

---

## Citation-resolution note (read before checking citations)

- `src/puffo_agent/…py` — a path in **this** worktree. Every such path in this review resolves
  (`test -e`); they are the grep-checkable local citations.
- `puffo-agent @ fleet/fat-cloud-phase1 (PR #127)` — phase-1 code (`agent/bridge_client.py`,
  the `state.py` transport keys, the `worker.py` transport branch) is **only** on that sibling branch and
  is referenced by short name, **never** with a `src/puffo_agent/` prefix (`bridge_client.py` does not
  exist under `src/puffo_agent/agent/` in this worktree — confirmed absent; and `portal/state.py` here has
  no `transport` token at all). Line numbers for phase-1 claims (`state.py:948`, `worker.py:363`) index the
  sibling branch, not this tree.

**Local citation manifest** — every in-worktree file this review relies on (all resolve; `test -e`):
`src/puffo_agent/agent/core.py`, `src/puffo_agent/agent/memory.py`, `src/puffo_agent/agent/skills_loader.py`,
`src/puffo_agent/agent/puffo_core_client.py`, `src/puffo_agent/agent/message_store.py`,
`src/puffo_agent/agent/status_reporter.py`, `src/puffo_agent/agent/file_browser.py`,
`src/puffo_agent/agent/model_catalog.py`, `src/puffo_agent/agent/shared_content.py`,
`src/puffo_agent/agent/events.py`, `src/puffo_agent/agent/providers/anthropic_provider.py`,
`src/puffo_agent/crypto/keystore.py`, `src/puffo_agent/crypto/http_client.py`,
`src/puffo_agent/crypto/ws_client.py`, `src/puffo_agent/crypto/http_auth.py`,
`src/puffo_agent/crypto/attachments.py`, `src/puffo_agent/mcp/puffo_core_tools.py`,
`src/puffo_agent/mcp/puffo_core_server.py`, `src/puffo_agent/mcp/host_tools.py`,
`src/puffo_agent/mcp/data_client.py`, `src/puffo_agent/hooks/permission.py`,
`src/puffo_agent/macos/keychain.py`, `src/puffo_agent/portal/daemon.py`, `src/puffo_agent/portal/worker.py`,
`src/puffo_agent/portal/rpc_service.py`, `src/puffo_agent/portal/runtime_matrix.py`,
`src/puffo_agent/portal/state.py`, `src/puffo_agent/portal/credential_refresh.py`,
`src/puffo_agent/portal/data_service.py`. *(The short forms used in the tables below — `agent/core.py`,
`crypto/keystore.py`, … — are these same paths minus the package-root prefix, per the desktop-doc
convention; `worker.py:363`/`state.py:948` are the exception, indexing the phase-1 branch as noted above.)*
- `puffo-server …` — the puffo-server monorepo. The design docs the subject cites resolve there, **not**
  under the standalone `roadmap/cloud-agent/` mirror. Where each was verified:
  - `BRIDGE-COVERAGE-AUDIT.md` → `puffo-server/roadmap/cloud-agent/BRIDGE-COVERAGE-AUDIT.md` (main `dev`).
  - `BRIDGE-WIRE-PROTOCOL.md`, `MCP-TOKEN-AUTH-AUDIT.md`, `REAL-E2B-E2E-RESULTS.md`,
    `BRIDGE-CRYPTO-DESIGN-V2.md` → puffo-server feature-branch worktrees
    (`fleet/.worktrees/cloud-agent-*`). All four **exist and were read**; they are absent from the
    standalone `roadmap/cloud-agent/` mirror, which is a partial copy. The subject's cross-repo labeling
    (`puffo-server roadmap/cloud-agent/<DOC>.md`) is therefore **correct** — these are not dangling
    references.

---

## 0. Ground-truth census `G` (reconstructed from code, before re-reading the doc)

Enumerated from `src/puffo_agent/` packages/modules + external touchpoints, independent of the subject.
LOC via `wc -l`. This is the yardstick for M1 and for the delta-from-desktop set `D` (§5).

**Top-level packages (11):** `agent/` (+ `adapters/`, `harness/`, `providers/`, `skills/`), `crypto/`
(14 modules, 1578 LOC), `mcp/` (8 modules), `hooks/`, `macos/`, `portal/` (+ `api/` 8, `control/` 9,
`ws_local/` 14, `ui/` many). Root: `__init__.py`, `_proc.py`.

**Load-bearing modules (the census diff hinges on these):**

| Subsystem | Code (`file:line`, LOC) | In doc's KEEP/SWAP/ADD/delta? |
|---|---|---|
| Turn loop | `agent/core.py:71` `PuffoAgent`; delegates at `:314` `adapter.run_turn` (529 LOC) | ✅ KEEP |
| Adapters (7 kinds) | `agent/adapters/base.py:65`; `chat_only`/`sdk`/`cli_session`/`codex_session`/`local_cli`/`docker_cli` | ✅ KEEP |
| Harnesses (4) | `agent/harness/{claude_code,codex,gemini_cli,hermes}.py` | ✅ KEEP |
| Runtime×harness×provider matrix | `portal/runtime_matrix.py` (5 runtimes, 4 harnesses, 3 providers; `DEFAULT_HARNESS_FOR_PROVIDER:105`) | ✅ KEEP (named) |
| Direct model providers | `agent/providers/anthropic_provider.py`, `openai_provider.py` | ❌ not named |
| System-prompt assembly | `agent/shared_content.py` (1390 LOC; `ensure_shared_primer`, `assemble_claude_md`) | ❌ not named |
| Flat memory | `agent/memory.py:6` `MemoryManager` (37 LOC) | ✅ KEEP |
| Skills | `agent/skills_loader.py:8` `SkillsLoader` + `skills/` | ✅ KEEP |
| Model catalog / options | `agent/model_catalog.py:26` `ModelOption` (202 LOC; refreshes from `api.anthropic.com/v1/models:88`) | ❌ not addressed |
| MCP core tools (17) | `mcp/puffo_core_tools.py:368` `register_core_tools`, `@mcp.tool` ×17 | ✅ KEEP |
| MCP host/install tools (7) | `mcp/puffo_core_server.py` (install/uninstall skill+MCP, refresh); `mcp/host_tools.py` | ✅ KEEP (implied) |
| E2E crypto stack | `crypto/` 14 modules: `message.py`, `primitives.py`, `keystore.py:80`, `http_auth.py:44`, `attachments.py`, … | ✅ SWAP |
| Signed HTTP/WS gates | `crypto/http_client.py:26` `PuffoCoreHttpClient`, `crypto/ws_client.py:41` `PuffoCoreWsClient` | ✅ SWAP |
| Message ingress/egress client | `agent/puffo_core_client.py` (3756 LOC; decrypt `:630`) | ✅ (in flows) |
| **Local message store / history** | `agent/message_store.py:120` `MessageStore` (`messages.db`, 637 LOC) + `portal/data_service.py` + `mcp/data_client.py` + history tools `puffo_core_tools.py:531/597/628` | ⚠️ **only as ADD `fetch_pending`; local store omitted** |
| Signed events | `agent/events.py` (`SignedEvent` → `/spaces/events`) | ❌ not named |
| Portal daemon/worker lifecycle | `portal/daemon.py`, `portal/worker.py`, `portal/rpc_service.py` | ✅ KEEP |
| **Status/error/heartbeat reporting** | `agent/status_reporter.py:32` `StatusReporter` (idle/busy/error → `self._http.post`, 234 LOC) | ❌ **omitted — see G3** |
| Credential (OAuth) refresh | `portal/credential_refresh.py` (1268 LOC, single-writer) + `macos/keychain.py` | ❌ not addressed |
| PreToolUse permission gate | `hooks/permission.py` (268 LOC; DMs operator, fails open) | ❌ not named |
| Workspace file IO | `agent/file_browser.py:47` `FileBrowser` (read-only over WS RPC, 106 LOC) | ❌ not addressed |
| Config → behavior | `portal/state.py` `agent.yml` + `runtime_matrix.py:146` `validate_triple` | ✅ KEEP + ADD (live config) |
| Desktop-only surfaces | `portal/ws_local/` (14), `portal/control/` (9), `portal/api/` (8), `portal/ui/`, `macos/` | ❌ not marked drop-by-design |

**External touchpoints (from code):** puffo-server bridge (`portal/state.py:941`
`DEFAULT_PUFFO_SERVER_URL="https://chat.puffo.ai/relay"`); Anthropic/OpenAI/Google **direct** SDKs
(`ANTHROPIC_API_KEY`, `agent/adapters/sdk.py:91`); claude/codex/gemini/hermes CLIs; Docker; macOS Keychain.
**Not present in the tree (correctly design-only in the doc):** `AIM`, `LiteLLM`, `ANTHROPIC_BASE_URL`,
`E2B` — grep returns **zero** hits for each in `src/puffo_agent/`.

---

## 1. The three hard metrics

| Metric | Value | Basis |
|---|---|---|
| **Coverage** (`\|covered\|/\|G\|`) | **≈ 0.72** (21 of 29 cloud-relevant subsystems) | §0 census; 8 load-bearing omissions (1 gating) |
| **Accuracy** (verified/sampled) | **18/20 ≈ 0.90** (≥15 sampled) | §2 sample table; 1 material false + minor imprecisions |
| **Data flows correct** | **8 / 9** | §3; flow 4 (provisioning+lifecycle) present-partial |

The doc clears the M2 accuracy gate (~0.85). It does **not** clear M1 (a load-bearing omission) or M6
(mis-stated statuses) — those drive the verdict.

---

## 2. The seven rubric dimensions (score 0–5 + findings)

### M1 — Coverage · **3 / 5** · coverage ≈ 0.72

The doc covers the **swap-relevant** surface very well (~95% of what a transport-swap doc must name), but
measured as an architecture census (what M1 demands) it omits **8 load-bearing subsystems**, one of which
is materially affected by the swap:

- **Gating omission — `agent/status_reporter.py:32`** (status/error/heartbeat → operator). See **G3** (§7).
- Notable omissions: **local message store / history** (`agent/message_store.py:120`,
  `portal/data_service.py`, `mcp/data_client.py`, history tools) represented only as an *ADD*
  (`fetch_pending`), never as the KEPT local `messages.db`; **`portal/credential_refresh.py`** (OAuth
  refresh); **`agent/model_catalog.py:26`**; **`agent/file_browser.py:47`**; **`agent/providers/`**;
  **`agent/shared_content.py`** (the actual system-prompt assembly).
- Minor: `hooks/permission.py`, `agent/events.py`, `agent/disk_cache.py`.
- The desktop-only surfaces (`portal/ws_local/`, `control/`, `api/`, `ui/`, `macos/`) are reasonably
  out-of-scope for the cloud shape, but the doc never says so — the delta table lists *some*
  drop-by-design rows and silently skips these.

The doc is explicitly a **swap** doc ("exactly one thing swapped"), so these omissions are scoping, not
error — but per the rubric M1 and the task's cross-comparison purpose, a load-bearing observability
subsystem missing is gating.

### M2 — Accuracy · **4 / 5** · verified 18/20 ≈ 0.90

In-worktree code citations are **exceptionally precise**. Sample (≥15 required):

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `crypto/` = 14 modules, ~1,578 LOC | ✅ exact | `ls crypto/*.py \| wc -l`=14; `wc -l`=1578 |
| 2 | `PuffoAgent` at `core.py:71` | ✅ | `agent/core.py:71` |
| 3 | delegates each turn to `Adapter` `core.py:82` | ⚠️ imprecise | `:82` is a **docstring** line; real call is `:314` `adapter.run_turn` |
| 4 | `register_core_tools` `puffo_core_tools.py:368`, ~17 tools | ✅ exact | `:368`; `@mcp.tool` count = **17** |
| 5 | seal at `puffo_core_tools.py:512` | ⚠️ understated | `:512` real, but **3** seal sites exist (`:512`, `:1127`, fallback `puffo_core_client.py:3728`) |
| 6 | decrypt at `puffo_core_client.py:630` | ✅ | `:630` (also `:643`) |
| 7 | `MemoryManager` `memory.py:6` | ✅ | `agent/memory.py:6` |
| 8 | keys under `agents/<id>/keys` `keystore.py:86` | ✅ (class at `:80`) | `:86` is the `for_agent` keys-path line — apt for the prose |
| 9 | `sign_request` `crypto/http_auth.py` | ✅ | `http_auth.py:44` |
| 10 | `DEFAULT_HARNESS_FOR_PROVIDER` `runtime_matrix.py:105` | ✅ exact | `:105` |
| 11 | `agent/bridge_client.py` ~290 LOC, imports nothing from `crypto/` | ✅ (phase-1) | 290 LOC; `crypto` appears only in its docstring |
| 12 | `VALID_TRANSPORTS=("native","bridge")`; bridge needs `server_url`+`sandbox_token` | ✅ (phase-1) | `portal/state.py:948`; validation `:1095` |
| 13 | worker selects bridge at `portal/worker.py:363` | ✅ (phase-1) | `if pc.transport=="bridge"` at `:363`; `CloudBridgeClient(...)` at `:371` |
| 14 | `lifecycle.rs:365 TODO(phase-2)` ConfigUpdate + AIM forward not built | ✅ | verbatim TODO; no `ConfigUpdate` variant exists |
| 15 | live config edits "the mutable subset (soul/provider/model)" | ⚠️ understated | `PatchCloudAgentRequest` `lifecycle.rs:152` has **7** mutable fields (adds display_name/avatar_url/role/role_description) |
| 16 | `select_provisioner_kind` DirectE2b→AIM→Stub, `E2B_DIRECT=1` | ✅ | order at `direct_e2b.rs:106`; gate `lib.rs:208`; (`:38` is the doc-comment) |
| 17 | REAL-E2B: plain `curl` → `GET /spaces 200`; LLM plane not exercised; DirectE2b #155 | ✅ all three | `REAL-E2B-E2E-RESULTS.md:39–45,49–51` |
| 18 | COVERAGE-AUDIT §6 #1/#2 = backfill+ack; §6.4 reactions | ✅ | audit content matches |
| 19 | MCP-TOKEN-AUTH §3 `SubkeyOrTokenAuth`/`resolve_agent_by_token`, 4 routes | ✅ (name nuance) | merged as `SubkeyOrSandboxTokenAuth` on config-crud |
| 20 | **server seal/open are `[DESIGNED]` / not merged** | ❌ **FALSE** | `seal_agent_message`/`open_agent_message` **merged** in `puffo-server dev`: `server/src/cloud_agent/bridge.rs:447`, `:692` — see **G1** |

Verified 18/20. Item 20 is a material mis-statement (also the M6 gating finding); items 3/5/15 are minor
imprecisions. Notably, the doc's own **honest omission** of the "+230/−2070" figure (it declined to repeat
an unverifiable number and quantified from the tree instead) is correct and to its credit.

### M3 — Data-flow completeness · **8 / 9**

| # | Flow | Verdict |
|---|---|---|
| 1 | Message IN (server open → bridge `Message` → turn) | present-correct |
| 2 | Message OUT (reply → server seal → `send` frame) | present-correct (understates 3 seal sites) |
| 3 | Think-path (harness/adapter → LLM) | present-correct, honestly `[DESIGNED]` (LLM plane not exercised) |
| 4 | **Provisioning + lifecycle** | **present-partial** — create/connect drawn; agent-side **reconnect + heartbeat + stop + status** (rubric lists "reconnect/heartbeat") not represented, and status reporting has **no cloud path** (G3) |
| 5 | Config → behavior (`agent.yml` transport select; live config) | present-correct |
| 6 | Memory (flat) | present-correct, honestly notes M1–M4 are not literal tiers |
| 7 | Auth / trust boundary (keyless `x-sandbox-token`) | present-correct — the doc's strongest flow |
| 8 | MCP tools + skills | present-correct |
| 9 | External deps wiring (LiteLLM/AIM/E2B/CLIs) | present-correct, labels honest |

Flow 4 is the only miss (counted against): the cloud lifecycle diagram stops at `connect → cfg` and never
shows respawn/reconnect/heartbeat/status — the same seam as G3.

### M4 — Boundary/seam correctness · **4 / 5**

The **primary seams are drawn correctly and at the right altitude** — this is the doc's core strength:

- **Transport/crypto swap seam** (the swap point): correct. Delete `crypto/` + `keys/`; select
  `transport: "bridge"`; the two encrypt/decrypt call sites become plaintext send/recv. This is the right
  architectural boundary and it is drawn cleanly.
- **Keyless-sandbox trust boundary**: correct — `x-sandbox-token` egress injection + server-side crypto,
  cited to `BRIDGE-WIRE-PROTOCOL.md §2.2` (verified).
- **Fat-vs-thin**: correct — "same cognitive codebase, two transports."

Deduction: the **adapter↔runtime-matrix seam** the rubric explicitly requires
(`chat-local/sdk/cli-local/cli-docker/ws-local × claude-code/codex/hermes/gemini`) is **under-developed**.
The doc collapses it to a "claude-code CLI vs chat-local bake-off" and never lays out the 5-runtime ×
4-harness × 3-provider matrix that `portal/runtime_matrix.py` actually encodes, nor which runtime identity
the sandbox assumes (`cli-local`? the reserved `cli-sandbox`?). No **wrong** seam is drawn — this is
incompleteness, not error.

### M5 — Diagram↔doc↔code tri-consistency · **4 / 5**

Four Mermaid blocks; all parse; legend present; grouped by subsystem; not a hairball. Nearly every node/edge
traces to real code (spot-checked): `keystore.py`, `http_client.py`, `ws_client.py`,
`puffo_core_tools.py:512`, `puffo_core_client.py:630`, `runtime_matrix.py`, `daemon.py`/`worker.py` all
resolve. Findings:

- **Simplified nodes:** single `encrypt (:512)` / `decrypt (:630)` nodes stand in for 3 real seal sites +
  2 decrypt sites — a legible simplification, worth a footnote.
- **Unrepresented claim:** `agent/shared_content.py` (the real CLAUDE.md/system-prompt assembly) has no
  node; `mem` cites only `memory.py` (37 LOC of topic notes).
- **Designed edges honestly labeled:** `harness → LiteLLM` and `aim → provisions` have no code path in the
  tree, but each is tagged `[DESIGNED]`, so they are not "invented edges."

### M6 — Currency/honesty · **2 / 5** · mis-stated-status count = **≥ 2** (target 0)

**Intent is exemplary** — the doc labels every claim `[BUILT]/[DESIGNED]/[BLOCKED-ON-NEW-SERVER-FRAME]`,
disclaims the +230/−2070 figure, disclaims memory M1–M4, and attributes phase-1 to the sibling branch. In
isolation that discipline earns a 5. It is knocked down hard because **the code moved ahead of the design
docs the author trusted for build-status**, producing ≥2 mis-stated statuses on **core** claims — the
rubric's explicit fail condition ("described as BUILT that isn't, **or vice versa**"):

- **G1 — server seal/open labeled `[DESIGNED]`/not-merged → actually `[BUILT]`.** `seal_agent_message` and
  `open_agent_message` are merged in `puffo-server dev`: `server/src/cloud_agent/bridge.rs:447`, `:692`
  (imported `bridge.rs:33`), with `NO_SUBKEY` implemented (`bridge.rs:172`). The doc marks this `[DESIGNED]`
  in ~5 places (overview Mermaid, SWAP prose, trust boundary, sequence diagram, phase table). Corollary:
  the doc's "a `send` returns `NO_SUBKEY` **until seal/open land**" inverts the cause — seal/open **have**
  landed; `NO_SUBKEY` now fires on **missing subkey-seed provisioning**, a narrower residual gap.
- **G2 — backfill/read-ack labeled `[DESIGNED]` (Phase 3) → actually `[BUILT]`.** `AgentClientMsg::FetchPending`
  (`bridge.rs:76`) and `AgentClientMsg::Ack{envelope_ids}` (`bridge.rs:82`) are merged in `dev`
  (commit `6725d46`, an ancestor of `dev` HEAD). The doc calls the server side "not-yet-covered."

Honest labels that **hold up** (adjudicated per criterion 8):
- **LLM plane / `/v1/llm/complete` retirement → honest `[DESIGNED]`.** No such route exists in the fat tree;
  `/v1/llm/complete` is the *thin* runtime's path, decided-retired in favor of sandbox-direct LiteLLM;
  `REAL-E2B-E2E-RESULTS.md` confirms it was "not exercised." Correct.
- **Memory M1–M4 → honest.** `agent/memory.py` is 37 LOC of flat topic memory; the doc explicitly says
  M1–M4 are not literal tiers. Correct.
- **Phase-1 `bridge_client.py` → honest-but-generous.** Verified 290 LOC, no `crypto/` import, on
  `fleet/fat-cloud-phase1` (PR #127). Labeling open-PR-branch code `[BUILT]` ("merged code you can run
  today") is at the generous edge of the legend, but it is **disclosed** (the branch is named), so it is a
  caveat, not a mis-statement.

### M7 — Navigability · **4 / 5**

Legend-first, entry points labeled, KEEP/SWAP/ADD is a strong reading order, citations are dense. Three
"find where X happens" probes actually run:

1. *Where is an inbound message decrypted?* → SWAP §, `puffo_core_client.py:630`. **< 20s.** ✅
2. *Where is the transport selected?* → SWAP §, `portal/worker.py:363` (attributed to PR #127). **< 20s.** ✅
3. *Where does the agent report status/errors to the operator?* → **not findable** (omitted). ❌

2 of 3 probes are fast; the third is unanswerable, tracking the coverage gap.

---

## 3. Data-flow summary → **8 / 9** (see M3 table above)

## 4. Rubric scorecard

| Dim | Name | Score |
|---|---|---|
| **M1** | Coverage | **3 / 5** (≈0.72) |
| **M2** | Accuracy | **4 / 5** (18/20 ≈ 0.90) |
| **M3** | Data-flow /9 | **8 / 9** |
| **M4** | Seam-correctness | **4 / 5** |
| **M5** | Tri-consistency | **4 / 5** |
| **M6** | Currency/honesty | **2 / 5** (≥2 mis-stated) |
| **M7** | Navigability | **4 / 5** |

---

## 5. Delta-from-desktop cross-comparison (deliverable core)

**Reconstruction method.** `D` = the desktop fat-agent capability set, built independently from
`src/puffo_agent/` code (§0 census) + the desktop arch doc `fleet/pyagent-arch-doc:docs/ARCHITECTURE.md`
(its §2 component list, §5 nine flows, §12 "Where does X happen?" index). `CLOUD-VS-DESKTOP-GAP.md` is used
**only as a labeled secondary cross-reference**, with its caveat noted: its "desktop" is the older
TypeScript `agent-core` and its "cloud" is the retired **thin** runtime
(`packages/puffo-agent-cloud`) — so a "gap" there is typically a **KEEP** in the fat-cloud (the whole fat
stack runs in-sandbox). Each `d∈D` is classified **KEEP / SWAP / ADD / DROP-BY-DESIGN / MISSING-GAP** with
a desktop citation, then diffed against the subject's delta table (`docs/FAT-CLOUD-ARCHITECTURE.md`
lines 416–438).

**Completeness verdict on the subject's delta table.** The subject claims (line 412): "Every capability
row from `BRIDGE-COVERAGE-AUDIT.md §3` is represented, so the table is complete." That claim is **true but
narrowly scoped** — `BRIDGE-COVERAGE-AUDIT.md §3` is a **message-surface** audit (Send/Receive/Ack/backfill/
attachments/reactions/metadata). It does **not** enumerate the agent's *local/cognitive/observability*
capabilities. Reconstructed against the full `D`, the delta table **omits or misclassifies ≥5 desktop
capabilities** (satisfying rubric criterion 7(a)). Reconstructed table:

| # | Capability (`d∈D`) | Desktop citation | Classification | In subject's delta table? |
|---|---|---|---|---|
| 1 | Turn loop / adapters / harness | `agent/core.py:71`, `adapters/base.py:65` | **KEEP** | ✅ |
| 2 | Runtime×harness×provider matrix | `portal/runtime_matrix.py:105/146` | **KEEP** | ✅ (as part of #1) |
| 3 | Direct model providers | `agent/providers/anthropic_provider.py` | **KEEP** (auth → LiteLLM) | ❌ omitted |
| 4 | System-prompt assembly (CLAUDE.md) | `agent/shared_content.py` | **KEEP** | ❌ omitted |
| 5 | Flat memory | `agent/memory.py:6` | **KEEP** | ✅ |
| 6 | Skills | `agent/skills_loader.py:8` | **KEEP** | ✅ |
| 7 | MCP core tools (17) | `mcp/puffo_core_tools.py:368` | **KEEP** (contracts) | ✅ |
| 8 | MCP host/install tools (7) + custom MCP install | `mcp/puffo_core_server.py`, `mcp/host_tools.py`, `adapters/desired_install.py` | **KEEP** | ✅ (as part of #7) |
| 9 | **Model catalog / options** | `agent/model_catalog.py:26` | **KEEP-with-reconcile** (LiteLLM shifts the list) | ❌ omitted → **GAP #4** |
| 10 | Key custody | `crypto/keystore.py:80` | **SWAP** (sandbox holds nothing) | ✅ |
| 11 | Signed HTTP/WS gates | `crypto/http_client.py:26`, `crypto/ws_client.py:41` | **SWAP** | ✅ |
| 12 | Send DM / channel | seal `mcp/puffo_core_tools.py:512` | **SWAP** (server seal — **now [BUILT]**, G1) | ✅ (mislabeled `[DESIGNED: server seal]`) |
| 13 | Receive (live push) | decrypt `agent/puffo_core_client.py:630` | **SWAP** (server open — **now [BUILT]**, G1) | ✅ (mislabeled) |
| 14 | Send-ack / presence heartbeat | `crypto/ws_client.py` | **SWAP** | ✅ |
| 15 | Backfill / history (cold-start) | `GET /messages/pending` | **ADD** (`fetch_pending` — **now [BUILT] server-side**, G2) | ✅ (mislabeled `[DESIGNED]`) |
| 16 | Read-ack | `POST /messages/ack` | **ADD** (`ack` — **now [BUILT] server-side**, G2) | ✅ (mislabeled) |
| 17 | Metadata reads (spaces/channels/members/profiles) | signed `GET /spaces…` | **ADD** (token-HTTP `SubkeyOrSandboxTokenAuth`) | ✅ |
| 18 | **Local message store / history tools** | `agent/message_store.py:120` + `portal/data_service.py` + `mcp/data_client.py` + `puffo_core_tools.py:531/597/628` | **KEEP** (local `messages.db`) | ❌ misclassified as pure ADD → **GAP #3** |
| 19 | Signed events (`/spaces/events`) | `agent/events.py` | **ADD** (needs token-HTTP) | ❌ omitted |
| 20 | Threads / `root_id` | n/a on bridge | **ADD / BLOCKED** | ✅ |
| 21 | Attachments | `crypto/attachments.py` + `/blobs/*` | **ADD / BLOCKED** | ✅ |
| 22 | Reactions | absent server-wide | **ADD / BLOCKED** | ✅ |
| 23 | Msg edit / delete / typing / read-receipt | absent server-wide | **DROP-BY-DESIGN** | ✅ |
| 24 | Space/channel metadata edit/delete | `PATCH`/`DELETE /spaces…` | **DROP-BY-DESIGN** | ✅ |
| 25 | Identity/device/subkey rotation over wire | local key ops | **DROP-BY-DESIGN** | ✅ |
| 26 | Portal daemon/worker lifecycle | `portal/daemon.py`, `portal/worker.py` | **KEEP** | ✅ |
| 27 | Config → behavior + live config | `portal/state.py`, `runtime_matrix.py:146`; ADD `lifecycle.rs:365` | **KEEP** + **ADD** | ✅ |
| 28 | **Status/error/heartbeat reporting** | `agent/status_reporter.py:32` (`self._http.post`) | **MISSING-GAP** | ❌ omitted → **GAP #1** |
| 29 | **Credential (OAuth) refresh** | `portal/credential_refresh.py` + `macos/keychain.py` | **DROP-BY-DESIGN?** (unresolved) | ❌ omitted → **GAP #2** |
| 30 | **Workspace file IO** | `agent/file_browser.py:47` | **KEEP** (exposure = open call) | ❌ omitted → **GAP #5** |
| 31 | PreToolUse permission gate | `hooks/permission.py` | **KEEP** (or drop-by-design) | ❌ omitted → **GAP #6** |
| 32 | Desktop-only: ws-local / control plane / local API / PyQt UI / macOS keychain | `portal/ws_local/`, `control/`, `api/`, `ui/`, `macos/` | **DROP-BY-DESIGN** | ❌ not marked drop |

Every row carries exactly one classification and a desktop citation; the four modules the task names
explicitly — `message_store.py` (#18), `status_reporter.py` (#28), `file_browser.py` (#30),
`model_catalog.py` (#9) — are each classified with a reason.

---

## 6. Ranked MISSING-GAP list — **count = 6**

Ordered by impact. Each: what desktop has (+cite), verdict (real gap → improvement, or OK-to-drop →
mark drop-by-design + why), impact rank.

1. **[HIGH · real gap] Status/error/heartbeat reporting — `agent/status_reporter.py:32`.**
   Desktop `StatusReporter` reports idle/busy/error and per-message runs to the server via the **signed
   HTTP client** (`self._http` = `PuffoCoreHttpClient`, `status_reporter.py:14/88/120/190`). It therefore
   sits **on** the transport being deleted, not above it — falsifying the doc's "everything above the seam
   is unchanged" for this module. The keyless bridge frame set has **no status/error frame** (only
   `Heartbeat` liveness — `BRIDGE-COVERAGE-AUDIT.md §1`), corroborated by `CLOUD-VS-DESKTOP-GAP.md`
   ("no logs/status surface", P2). None of Phases 1–4 addresses it. **→ Improvement:** add an
   `AgentClientMsg::Status/Error` bridge frame (or a token-HTTP status route), and classify status
   reporting in the doc as **SWAP-needs-new-frame** — not a silent KEEP. *Not OK to drop: it is the
   operator's only observability into a headless cloud agent.*

2. **[MED · resolve the design call] OAuth credential refresh — `portal/credential_refresh.py` (+ `macos/keychain.py`).**
   Desktop's daemon refreshes Claude/Codex OAuth (single-writer, 1268 LOC). In the cloud the model plane
   swaps to `ANTHROPIC_BASE_URL` → LiteLLM virtual-key (`[DESIGNED]`), which likely makes OAuth refresh
   moot. **→ Verdict:** most likely **DROP-BY-DESIGN**; the doc should say so **and why** (VK replaces
   per-CLI OAuth) — or classify it **SWAP** if the in-sandbox `claude-code` CLI harness still authenticates
   by OAuth. Today the doc is silent, leaving an unresolved seam.

3. **[MED · misclassification] Local message store + history tools — `agent/message_store.py:120`.**
   Desktop persists every message to `messages.db` and serves history locally
   (`portal/data_service.py`, `mcp/data_client.py`, tools `get_channel_history`/`get_dm_history`/
   `get_thread_history` at `puffo_core_tools.py:531/597/628`). The delta table represents history **only**
   as an ADD (`fetch_pending`), implying desktop history was a server round-trip. **→ Improvement:** add a
   **KEEP** row for local persistence + history tools, and clarify that `fetch_pending` is **cold-start
   backfill on top of** the retained local store (a fresh sandbox boots with an empty `messages.db`).
   *Functionally a KEEP; the gap is in the analysis.*

4. **[MED · undocumented reconcile] Model catalog / options — `agent/model_catalog.py:26`.**
   Desktop refreshes a concrete per-provider model list from `api.anthropic.com/v1/models`
   (`model_catalog.py:88`). Under LiteLLM the reachable aliases/models are the VK's set. **→ Improvement:**
   note how `model_catalog` reconciles with the LiteLLM virtual-key model set (KEEP-with-reconcile).

5. **[LOW · open design call] Workspace file IO — `agent/file_browser.py:47`.**
   Desktop read-only file browser over WS RPC (`ALLOWED_ROOTS=("memory","skills","agents")`). In-sandbox it
   KEEPs, but operator exposure interacts with the E2B security posture (`CLOUD-VS-DESKTOP-GAP.md`:
   "design call needed", P2). **→ Improvement:** a one-line disposition (KEEP-in-sandbox / expose-or-not).

6. **[LOW · omitted keeps] Permission gate + providers + prompt-assembly + events.**
   `hooks/permission.py` (PreToolUse gate; its operator-DM y/n semantics may even be **drop-by-design** for
   an autonomous agent), `agent/providers/`, `agent/shared_content.py`, `agent/events.py` are clean KEEPs
   absent from the enumerated KEEP list. **→ Improvement:** one summarizing sentence, or mark
   `permission.py` drop-by-design with reason.

---

## 7. Verdict + severity-ranked findings

# Verdict: NEEDS-REVISION

The subject is accurate (~0.90), draws the right primary seam, and is honest **in intent** — but two core
`[DESIGNED]` statuses are stale-as-`[BUILT]` (M6, gating) and a load-bearing observability capability is
both omitted and broken by the swap (M1/cross-comparison, gating). The fixes are bounded and additive
(re-verify status labels against `puffo-server dev`; add ~4 rows to the delta table). Recommend one revision
round, then re-review.

### Gating findings

- **G1 (M6) — Server seal/open mislabeled `[DESIGNED]`/not-merged; actually `[BUILT]`.**
  `seal_agent_message`/`open_agent_message` are merged in `puffo-server dev`
  (`server/src/cloud_agent/bridge.rs:447`, `:692`; `NO_SUBKEY` at `:172`). Fix: relabel to `[BUILT]`, and
  correct the `NO_SUBKEY` causal note — a `send` now fails on **missing subkey-seed provisioning**, not on
  unimplemented crypto.
- **G2 (M6) — Backfill/read-ack mislabeled `[DESIGNED]` (Phase 3); actually `[BUILT]`.**
  `AgentClientMsg::FetchPending` / `Ack` are merged in `dev` (`bridge.rs:76`, `:82`, commit `6725d46`).
  Fix: move Phase-3 backfill+ack to `[BUILT: server]` and re-scope Phase 3 to what actually remains
  (metadata token-HTTP on main, threads, attachments).
- **G3 (M1 + M3-flow-4 + cross-comparison) — `status_reporter.py` observability omitted and broken by
  the swap.** See MISSING-GAP #1. It rides the deleted signed-HTTP transport with no bridge equivalent;
  the fat-cloud, as designed, loses operator-facing status/error/heartbeat.
- **G4 (cross-comparison completeness) — Delta-table "complete" claim is scoped to the message-surface
  audit only.** Missing/misclassified desktop capabilities: local message store/history (#18),
  credential refresh (#29), model catalog (#9), file IO (#30), providers/prompt-assembly (#3/#4). Fix: add
  the KEEP/DROP-BY-DESIGN rows and re-word the completeness claim to "complete against the message surface;
  cognitive/observability capabilities are KEEP unless noted."

### Minor findings (non-gating)

- **m1** `core.py:82` cites a docstring line; the real turn delegation is `:314` (`adapter.run_turn`).
- **m2** The SWAP describes one OUT seal site (`:512`); there are **3** (`:512`, `:1127`, fallback
  `puffo_core_client.py:3728`) — the "delete two crypto call sites" framing is a slight undercount.
- **m3** Live config "mutable subset (soul/provider/model)" is actually **7** fields
  (`PatchCloudAgentRequest`, `lifecycle.rs:152`).
- **m4** Metadata-reads `[DESIGNED]` is branch-dependent: merged on `fleet/cloud-agent-config-crud` as
  `SubkeyOrSandboxTokenAuth` (doc's name `SubkeyOrTokenAuth`), not on `dev`. State the branch.
- **m5** `keystore.py:86` anchors the keys-path line; the `KeyStore` class is at `:80` (prose is fine).
- **m6** `direct_e2b.rs:38`, `worker.py:363`, `lifecycle.rs:365` cite doc-comment/branch-anchor lines with
  the implementation a few lines below — harmless but worth normalizing to the impl line.
- **m7** `[BUILT]` on open-PR-branch phase-1 code (PR #127) is at the generous edge of "merged code you can
  run today"; disclosed, so a caveat not an error.
- **m8** Desktop-only surfaces (`ws_local/`, `control/`, `api/`, `ui/`, `macos/`) are not marked
  drop-by-design; the delta table marks some drops but skips these.

### What the doc gets right (should be preserved in revision)

Precise in-worktree citations (crypto 14/1578 exact, 17 tools exact, every line cite resolves); the correct
narrow transport/crypto seam; the keyless trust-boundary treatment; the fat-vs-thin framing; the honest
disclaimers (+230/−2070, memory M1–M4, LLM-plane-not-exercised); and clean cross-repo/branch attribution of
`bridge_client.py` and the design docs.

---

*Reviewer note: this review writes only `docs/FAT-CLOUD-ARCHITECTURE-REVIEW.md`. It does not modify the
subject doc, product code, the `fleet/fat-cloud-phase1` branch, or puffo-server; no commit, push, or
self-merge. PR (if opened) is **held** for human review, base `fleet/fatcloud-arch-doc`.*
