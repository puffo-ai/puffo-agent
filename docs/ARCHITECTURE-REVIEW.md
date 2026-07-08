# Architecture-doc review — `docs/ARCHITECTURE.md` (puffo-agent)

> **Object under review:** `docs/ARCHITECTURE.md` (741 lines, 5 Mermaid diagrams),
> which claims to be cited `file:line` against tree tip `54895f3`.
> **Method:** the 7-dimension rubric at
> `roadmap/cloud-agent/ARCH-DOC-REVIEW-RUBRIC.md`.
> **Reviewer discipline (rubric core principle):** ground truth **G** was
> reconstructed from `src/puffo_agent/` *first* (§0 below), then the doc was diffed
> against G. Every finding cites code, not opinion.
> **Baseline integrity:** `git diff --name-only 54895f3 HEAD -- src` is **empty** — the
> doc commit (`284fa4f`) touched only `docs/`, so every `file:line` below is verified
> against the exact tree the doc claims (`54895f3`), not a drifted one.

---

## Verdict — **TRUSTWORTHY**

Canonical / reference-grade. No gating findings under the rubric's gating rules
(no M1 load-bearing omission; M2 accuracy 0.96 ≫ 0.85 floor; no absent/wrong M3
core flow; no M4 wrong seam; M6 mis-stated-status count = 0).

**3-line summary.** This is an unusually accurate, honestly-scoped doc: of ~70
distinct claims re-verified against code, exactly **one** is wrong (a ±1 line-number
drift) and **one** load-bearing sibling module is unnamed — both non-gating. Its
currency legend (BUILT / IN-FLIGHT / DESIGNED / RESERVED) is applied correctly
everywhere I checked, including the trap cases (LiteLLM / E2B / `x-sandbox-token` /
`/v1/llm/complete` — all **absent in this branch's code** and all correctly marked
DESIGNED/thin-only, never smuggled in as built). The right architectural seams are
drawn at the right altitude. Ship it as the canonical reference; the two findings are
polish, not blockers.

### The three hard metrics

| Metric | Value | Rubric gate | Pass? |
|---|---|---|---|
| **M1 coverage** `|covered|/|G|` | **51/52 ≈ 0.98** | no load-bearing omission | ✅ |
| **M2 accuracy** `verified/sampled` | **23/24 ≈ 0.96** (sampled = 24 ≥ 15) | ≥ ~0.85 | ✅ |
| **M3 flows** `correct/9` | **9/9** | no absent/wrong core flow | ✅ |

### Seven dimension scores

| Dim | Name | Score |
|---|---|---|
| **M1** | Coverage (subsystem census diff) | **5** / 5 |
| **M2** | Accuracy (claim verification) | **5** / 5 |
| **M3** | Data-flow completeness (9-flow checklist) | **5** / 5 |
| **M4** | Boundary/seam correctness (altitude test) | **5** / 5 |
| **M5** | Diagram↔doc↔code tri-consistency | **5** / 5 |
| **M6** | Currency / honesty | **5** / 5 |
| **M7** | Navigability | **5** / 5 |

---

## §0. Ground truth **G** (reconstructed from `src/puffo_agent/`, code-first)

`src/puffo_agent/` = 136 `.py` files. G is enumerated from the tree + grep, **before**
diffing the doc, per the rubric's anti-anchoring rule.

### G.1 — Top-level packages (6)
`agent/` · `crypto/` · `hooks/` · `macos/` · `mcp/` · `portal/`  → **all 6 present in the tree.**

### G.2 — Load-bearing subpackages (6)
`agent/adapters/` · `agent/harness/` · `agent/providers/` · `portal/api/` ·
`portal/control/` · `portal/ws_local/`.
(`agent/skills/` is an empty namespace — `__init__.py` only; the real loader is
`agent/skills_loader.py`.)

### G.3 — Load-bearing modules (census, 31 counted for the metric)
- **portal (9):** `daemon.py` `worker.py` `runtime_matrix.py` `state.py`
  `data_service.py` `rpc_service.py` `credential_refresh.py` `host_mcp_handler.py`
  `cli.py`
- **agent (7):** `core.py` `puffo_core_client.py` `message_store.py` `memory.py`
  `skills_loader.py` `status_reporter.py` `model_catalog.py`
- **agent/adapters + harness + providers (7):** `adapters/base.py` `adapters/cli_session.py`
  `adapters/codex_session.py` `adapters/desired_install.py` `harness/base.py`(+`__init__.build_harness`)
  `providers/anthropic_provider.py` `providers/openai_provider.py`
- **crypto (6):** `http_client.py` `ws_client.py` `message.py` `keystore.py`
  `http_auth.py` `certs.py`
- **mcp (4):** `puffo_core_server.py` `puffo_core_tools.py` `host_tools.py` `data_client.py`
- **hooks + macos (2):** `hooks/permission.py` `macos/keychain.py`
- **portal/api + control (load-bearing, doc-named):** `api/{server,auth,certs,ownership}.py`,
  `control/{envelope,machine_auth,agent_create,agent_message,reporter}.py`

Helper modules (`_time.py`, `_visibility.py`, `encoding.py`, `fingerprint.py`,
`event_kinds.py`, `hermes_helpers.py`, `mcp/_lifespan.py`, …) are **not** in G — the
rubric scores "helper missing = minor", and the doc explicitly states its altitude is
subsystem-level (`ARCHITECTURE.md:82-85`).

### G.4 — External touchpoints, **derived from `grep` on the code** (not from the rubric template)
The rubric's parenthetical example lists "puffo-server, AIM, LiteLLM, E2B, claude/codex
CLI." That is a *template*; G must reflect **this** repo. Grep results
(`grep -ril <term> src/puffo_agent`):

| Touchpoint | Code refs (files) | In G? |
|---|---|---|
| puffo-server (relay) | 19 | ✅ real |
| claude CLI | 48 | ✅ real |
| codex CLI | 29 | ✅ real |
| anthropic (SDK/provider) | 20 | ✅ real |
| hermes CLI | 15 | ✅ real |
| openai (SDK/provider) | 13 | ✅ real |
| gemini CLI | 11 | ✅ real |
| macOS Keychain | (`macos/keychain.py`) | ✅ real |
| **AIM** | **0 literal** | ✅ real **but external** — reached transitively via device-cert verification (`portal/api/certs.py`), never a direct import |
| **LiteLLM** | **0** | ❌ **NOT in G — absent in code** |
| **E2B** | **0** | ❌ **NOT in G — absent in code** |
| `ANTHROPIC_BASE_URL` | **0** | ❌ absent (the LiteLLM cloud think-path) |
| `x-sandbox-token` | **0** | ❌ absent (thin/`packages/` only, not on this branch) |
| `/v1/llm/complete` | **0** | ❌ absent (thin/`packages/` only) |

**Honesty note (rubric-critical):** because LiteLLM and E2B have **zero** code
references on this branch, listing them as *doc omissions* would be a false finding.
They are **not** in G. The doc's treatment of them (DESIGNED cloud touchpoints, no
code) is therefore **correct**, and no coverage finding below blames a service with no
`src/puffo_agent` reference.

---

## §M1 — Coverage (subsystem census diff) — **5/5 · |covered|/|G| = 51/52 ≈ 0.98**

Diff of G against the doc's prose + 5 diagrams:

| G slice | In G | Covered by doc | Metric |
|---|---|---|---|
| Top-level packages | 6 | 6 (each has a §2 subsection) | **6/6** |
| Load-bearing subpackages | 6 | 6 | **6/6** |
| Code-derived external touchpoints | 9 | 9 (§11 table + Diagram a) | **9/9** |
| Load-bearing modules | 31 | 30 | **30/31** |
| **Aggregate** | **52** | **51** | **51/52 ≈ 0.98** |

**Where each package lands in the doc** (greppable): `agent` → §2 "`agent/`" +
`ARCHITECTURE.md:127`; `crypto` → §2 "`crypto/`" + `:160`; `hooks` → §2 + `:198`;
`macos` → §2 + `:206`; `mcp` → §2 + `:181`; `portal` → §2 + `:87`. Subpackages
`agent/adapters` (`:138`), `agent/harness` (`:144`), `agent/providers` (`:149`),
`portal/api` (`:110`), `portal/control` (`:116`), `portal/ws_local` (`:114`) each have
prose.

**The one omission** → Finding **F2 (MINOR)**: `agent/adapters/codex_session.py` — the
long-lived codex `app-server` session manager — is not named in §2, even though its
structural sibling `cli_session.py` (the claude equivalent) **is** named
(`ARCHITECTURE.md:141-143`). Both are one-per-CLI-harness persistent-session managers;
`codex_session.py:1` self-describes as a "Lifecycle mirror of `cli_session.ClaudeSession`."
Given `codex` is a first-class harness in the §3 matrix, the asymmetry is a small
coverage gap. **Not gating** (helper-adjacent; the codex *harness* itself is covered).

No **load-bearing subsystem** is missing → M1 has **no gating omission**. Score **5/5**.

---

## §M2 — Accuracy (claim verification) — **5/5 · verified/sampled = 23/24 ≈ 0.96**

24 concrete claims/arrows sampled across **prose + all 5 diagrams**, each verified by
reading the cited code. (Citations are package-root-relative per the doc's convention,
i.e. `agent/core.py:314` = `src/puffo_agent/agent/core.py:314`.)

| # | Claim (doc) | Citation | Code check | Verdict |
|---|---|---|---|---|
| 1 | `class Daemon` | `daemon.py:64` | `64:class Daemon:` | ✅ |
| 2 | `_reconcile_once` diffs desired/running | `daemon.py:217` | `217:async def _reconcile_once` | ✅ |
| 3 | new agent → `Worker(...)` + `worker.start()` | `daemon.py:286` / `:295` | `286:worker = Worker(` `295:worker.start()` | ✅ |
| 4 | paused agents get `_report_lifecycle` | `daemon.py:568` | `568:async def _report_lifecycle` | ✅ |
| 5 | `build_adapter` maps `runtime.kind`→Adapter | `worker.py:89` | `89:def build_adapter` | ✅ |
| 6 | `RECONNECT_BACKOFF_SECONDS = 5.0` | `worker.py:86` | `86:RECONNECT_BACKOFF_SECONDS = 5.0` | ✅ |
| 7 | `class Worker` | `worker.py:561` | `561:class Worker:` | ✅ |
| 8 | worker heartbeat task | `worker.py:1489` | `1489:async def heartbeat():` | ✅ |
| 9 | reconnecting WS listen loop | `worker.py:1535` | `1535:while not self._stop.is_set():` → `client.listen(...)` | ✅ |
| 10 | `VALID_RUNTIMES` = 5 {chat-local,sdk-local,cli-local,cli-docker,ws-local} | `runtime_matrix.py:27` | frozenset of exactly those 5 | ✅ |
| 11 | `RESERVED_RUNTIMES` = {cli-sandbox} | `runtime_matrix.py:35` | `{RUNTIME_CLI_SANDBOX}` | ✅ |
| 12 | `HARNESS_PROVIDERS`: cc→anthropic; hermes→{anthropic,openai}; gemini-cli→google; codex→openai | `runtime_matrix.py:69` | exact map match | ✅ |
| 13 | `_HARNESS_BEARING_RUNTIMES` = {cli-local, cli-docker} | `runtime_matrix.py:79` | exact | ✅ |
| 14 | `validate_triple` rejects bad triples | `runtime_matrix.py:146` | `146:def validate_triple` | ✅ |
| 15 | `handle_message_batch` = turn entry | `core.py:143` | `143:async def handle_message_batch` | ✅ |
| 16 | `adapter.run_turn` runs the turn | `core.py:314` | `314:result = await self.adapter.run_turn(ctx)` | ✅ |
| 17 | inbound `handle_envelope` callback | `puffo_core_client.py:603` | `603:async def handle_envelope` | ✅ |
| 18 | IN decrypt via `decrypt_message` | `puffo_core_client.py:630` | `630:payload = decrypt_message(` | ✅ |
| 19 | OUT seal `encrypt_message_with_content_key` then `POST /messages` | `puffo_core_tools.py:512` / `:516` | `512:envelope, content_key = encrypt_message_with_content_key(` `516:...post("/messages"` | ✅ |
| 20 | fallback `send_fallback_message` seals with `encrypt_message` | `puffo_core_client.py:3609` / `:3728` | fn spans 3609–3749; `3728:envelope = encrypt_message(` inside it | ✅ |
| 21 | `_handle_frame` delivers frames | `ws_client.py:129` | `129:async def _handle_frame` | ✅ |
| 22 | ws-local mounted `GET /v1/ws-local` on 63387 | `ws_local/route.py:50` + `api/server.py:40` | `50:WS_LOCAL_PATH = "/v1/ws-local"`; `40:app.router.add_get(WS_LOCAL_PATH, handle_ws_local)` | ✅ |
| 23 | sdk-local requires anthropic api_key; CLI runtimes use `~/.claude/.credentials.json`, no api_key threaded | `worker.py:89`+ (build_adapter body) | code raises "sdk-local requires an anthropic api_key"; comment "CLI adapters authenticate via the host's ~/.claude/.credentials.json … no api_key is threaded" | ✅ |
| 24 | `build_harness` registry | `harness/__init__.py:20` | actual def is at **line 19**, not 20 | ⚠️ **OFF-BY-ONE** |

**Diagram coverage of the sample:** Diagram (a) → rows 18,19,21; Diagram (b) sequence →
15–21; Diagram (c) matrix → 10–13; Diagram (d) provisioning → 1–9; Diagram (e) swap →
18,19 + `ws_client.py:41`/`http_client.py:26` (both verified: `26:class
PuffoCoreHttpClient`, `41:class PuffoCoreWsClient`).

**The single miss** → Finding **F1 (NIT)**: `build_harness` is cited `harness/__init__.py:20`
but `def build_harness` is at **line 19**. Off by one line; still resolves to the right
symbol. **Correct fact:** `agent/harness/__init__.py:19`.

Additional deep-verified claims (not counted in the 24, all ✅): `message.py`
decrypt/encrypt at `:244`/`:120`/`:108`; matrix defaults `DEFAULT_PROVIDER_FOR_RUNTIME`
(all→anthropic, `:97`) and `DEFAULT_HARNESS_FOR_PROVIDER` (anthropic→claude-code,
openai→hermes, google→gemini-cli, `:105`); all four `supported_providers()` (claude_code
`:21`→{anthropic}, hermes `:29`→{anthropic,openai}, gemini_cli `:17`→{google}, codex
`:26`→{openai}); ports `state.py:782`=63386 / `:791`=63385 / `:807`=63387 with
`BridgeConfig.enabled = False` ("Off by default"); `hermes` = one-shot `hermes chat -q`
(`harness/hermes.py:4`); claude-code = stream-json + `--resume` (`cli_session.py`);
`migrate_legacy_kind` wired at `state.py:1046/1057`; api/certs verify trio
(`api/certs.py:23/57/94`); `agent_owner_root_pubkey` (`api/ownership.py:21`);
control `verify_control_cert`/`decrypt_command` (`control/envelope.py:38/66`);
`machine_cert` (`control/machine_auth.py:23`).

verified/sampled = **23/24 ≈ 0.96** ≫ 0.85 gate. Score **5/5**.

---

## §M3 — Data-flow completeness (9-flow checklist) — **5/5 · correct/9 = 9/9**

| # | Required flow | Where in doc | Status |
|---|---|---|---|
| 1 | Message **IN** (server→decrypt→turn) | §5.1 + Diagram (b) | **present-correct** — relay→`ws_client.py:41`→`_handle_frame:129`→`handle_envelope:603`→`decrypt_message:630`→`MessageStore:120`→batch→`handle_message_batch:143` (all verified) |
| 2 | Message **OUT** (reply→seal/send) | §5.1 + Diagram (b) | **present-correct** — `send_message:406`→`encrypt_message_with_content_key:512`→`POST /messages:516`; fallback `send_fallback_message:3609` |
| 3 | **Think-path** (harness/adapter→model) | §5.2 + Diagram (c) | **present-correct** — chat/sdk→provider SDK; cli→harness→vendor CLI (auth `~/.claude/.credentials.json`); ws-local→external engine |
| 4 | **Provisioning + lifecycle** | §6 + Diagram (d) | **present-correct** — `_reconcile_once:217`→`build_adapter:89`→`worker.start:295`→listen loop `:1535`→reconnect backoff `:86`; `_stop_worker:405`; heartbeat `:1489`; paused `_report_lifecycle:568` |
| 5 | **Config → behavior** | §7 | **present-correct** — `daemon.yml`/`agent.yml` (`state.py:684/709`)→`validate_triple:146`→`build_adapter:89`; legacy migration `:121` |
| 6 | **Memory** (M1–M4 tree + tools) | §10 | **present-correct** — BUILT flat `MemoryManager` (`memory.py:6/13/21/29`, verified 37-line flat impl); M1–M4 tree correctly marked IN-FLIGHT (0 hierarchical/recall code on this branch, grep-confirmed) |
| 7 | **Auth / trust boundary** | §8 | **present-correct** — `KeyStore:80`, `sign_request:44`, cert trio, ownership, control-envelope gate; DESIGNED keyless `x-sandbox-token` clearly separated |
| 8 | **MCP/tools + skills** | §9 | **present-correct** — `build_server:224`; `register_core_tools:368`; `DataClient:72`→63386; skills via `SkillsLoader:9` + `desired_install.py:127/214`; PreToolUse hook |
| 9 | **External deps wiring** | §11 | **present-correct** — 7-row table; BUILT deps cited, LiteLLM/E2B correctly DESIGNED |

All 9 present **and** correct. Score **5/5**.

---

## §M4 — Boundary/seam correctness (the altitude test) — **5/5 · 5/5 seams right-altitude**

| Required seam | Doc treatment | Altitude verdict |
|---|---|---|
| **Transport/crypto swap point** | §4 + §6 + Diagram (e): "two gate classes + exactly two message-crypto call sites"; before→after swap diagram | **RIGHT** — this is the load-bearing seam for the fat-cloud pivot and the doc makes it the spine. Verified: exactly 2 gate classes (`PuffoCoreHttpClient:26`, `PuffoCoreWsClient:41`) and 2 crypto sites (IN `puffo_core_client.py:630`, OUT `puffo_core_tools.py:512`). |
| **runtime × harness × provider matrix** | §3 + Diagram (c) | **RIGHT** — matches `runtime_matrix.py` byte-for-byte; correctly isolates that harness applies only to `{cli-local, cli-docker}`. |
| **Crypto gate classes** | §2 `crypto/` + §4 | **RIGHT** — transport (crypto-agnostic) vs message-crypto kept as distinct responsibilities, mirroring the code split. |
| **Fat-vs-thin distinction** | §"Fat vs thin" + BUILT/DESIGNED legend | **RIGHT** — fat `src/puffo_agent` (BUILT) vs thin `packages/` (IN-FLIGHT/FROZEN, not on branch) vs fat-cloud swap (DESIGNED). |
| **Keyless `x-sandbox-token` trust boundary** | §8 DIRECTION + Diagram (a) dotted | **RIGHT** — drawn as a *designed* boundary (dashed), not asserted as built; 0 `x-sandbox-token` refs in code confirms the honesty. |

No wrong seam, no wrong altitude (nothing degenerates into a file-listing or a
marketing box). Score **5/5**.

---

## §M5 — Diagram↔doc↔code tri-consistency — **5/5**

Traced every node/edge of Diagram (a) (the densest) and spot-traced (b)–(e):

- **Nodes → prose → code:** all 22 Diagram-(a) nodes trace to a §2/§4/§6 paragraph and
  to a real symbol (e.g. `pcc`→`puffo_core_client.py:603`, `msg`→`message.py`,
  `wsc`→`ws_client.py:41`, `tools`→`puffo_core_tools.py:406`). **0 orphan nodes.**
- **Edges → code path:** `wsc→pcc (_handle_frame:129)`, `pcc→msg (decrypt:630)`,
  `tools→msg (encrypt:512)`, `tools→httpc (POST /messages)`, `httpc→relay (signed
  HTTP)` all correspond to real call paths. **0 invented edges.**
- **Undrawn claims:** none material — every §5/§6 claim has a diagram home.
- **Legibility:** legend node in every diagram; grouped by subsystem via `subgraph`
  (portal/agent/crypto/mcp); solid=BUILT vs dashed=DESIGNED notation is consistent; 5
  focused diagrams instead of one hairball.

One cosmetic note → Finding **F3 (NIT)**: Diagram (a)'s `core -->|reply| tools` edge is
a container-level abstraction — the reply is emitted by the *engine* calling the MCP
`send_message` tool, not by `core.py` directly. Diagram (b) shows this precisely
(`ENG->TOOL`), so the two diagrams are consistent at their respective altitudes; no
correction needed, just noted. traceable/total ≈ 22/22 for nodes. Score **5/5**.

---

## §M6 — Currency / honesty — **5/5 · mis-stated-status count = 0**

Every status marker checked against code:

| Doc status claim | Code reality | Verdict |
|---|---|---|
| Fat `src/puffo_agent` daemon — **BUILT** | present, running | ✅ |
| Thin `packages/puffo-agent-cloud/` — **IN-FLIGHT / not on branch** | `ls packages/` → **absent** | ✅ |
| Fat-cloud `x-sandbox-token` swap — **DESIGNED** | `grep x-sandbox-token` → **0** | ✅ |
| Flat `MemoryManager` — **BUILT**; M1–M4 tree — **IN-FLIGHT** | `memory.py` = 37 flat lines; no recall/hierarchy code (grep 0) | ✅ |
| `api-puffo` runtime — **IN-FLIGHT**, *not* in `VALID_RUNTIMES` here | not in the frozenset; only worker comments `:838`,`:1235` | ✅ |
| `cli-sandbox` — **RESERVED** | `RESERVED_RUNTIMES = {cli-sandbox}` `:35` | ✅ |
| LiteLLM VK / `ANTHROPIC_BASE_URL` / E2B — **DESIGNED, no code** | grep 0 each | ✅ |
| `/v1/llm/complete` — thin-only, **retired** in fat direction | grep 0 in `src/` | ✅ |
| `BridgeConfig` bridge (63387) — **off by default** | `enabled: bool = False` (`state.py`) | ✅ |

Nothing aspirational is described as done; nothing built is described as missing.
mis-stated-status count = **0**. Score **5/5**.

---

## §M7 — Navigability — **5/5**

Three concrete "where does X happen?" probes, run against the doc, timed to answer with
`file:line`:

1. **"Where is an inbound message decrypted?"** → §12 index row 1 →
   `agent/puffo_core_client.py:630` (`decrypt_message`). Code-confirmed. **< 15 s.**
2. **"Which runtimes actually take a harness?"** → §3 prose + §12 →
   `portal/runtime_matrix.py:79` (`_HARNESS_BEARING_RUNTIMES` = {cli-local,cli-docker}).
   Code-confirmed. **< 30 s.**
3. **"Where is the PreToolUse permission decision made?"** → §12 →
   `hooks/permission.py:49`/`:57` (`_deny`/`_allow`). Code-confirmed. **< 20 s.**

All three answerable in ≪ 2 min. The doc ships a labeled reading order (§1), an 18-row
"Where does X happen?" index (§12), and per-section `file:line` cross-refs. Entry points
are named. Score **5/5**.

---

## Findings (severity-ranked)

Format: `[SEVERITY]` · `file:line` · **what** · why · **fix**.

1. **[NIT] F1 — `docs/ARCHITECTURE.md:145`** · **`build_harness` cited at
   `harness/__init__.py:20`, but the `def` is at line 19.** · A ±1 line-number drift;
   still resolves to the right symbol, but the doc's whole value proposition is exact
   citations. · **Fix:** change `:20` → `:19`.

2. **[MINOR] F2 — `docs/ARCHITECTURE.md:141-143`** · **`agent/adapters/codex_session.py`
   is unnamed in §2, while its sibling `cli_session.py` is named.** · `codex_session.py`
   is the codex `app-server` persistent-session manager — a structural mirror of
   `cli_session.ClaudeSession` (`codex_session.py:1-2`) — and `codex` is a first-class
   harness in the §3 matrix, so the omission is a real (if small) asymmetry. · **Fix:**
   add "…and `codex_session.py` (long-lived codex `app-server` JSON-RPC session, the
   codex mirror of `cli_session.py`)" to the `agent/adapters/` bullet.

3. **[NIT] F3 — `docs/ARCHITECTURE.md:286` (Diagram a)** · **`core -->|reply| tools`
   edge is a container-level abstraction** — the reply is emitted by the *engine*
   invoking the MCP `send_message` tool, not by `core.py` directly. · Diagram (b)
   already shows the precise `ENG->TOOL` hop, so the diagrams are consistent at their
   respective altitudes; readers taking Diagram (a) literally could over-read
   `core.py`'s role. · **Fix (optional):** relabel the edge `turn → reply` or add a
   one-word note; no correction strictly required.

### Adversarial search — what was probed (per rubric: prove the search when findings are thin)

Because the finding set is deliberately small, here is the search that produced it, so
"few findings" reads as *checked*, not *skimmed*:

- **~70 distinct `file:line` citations** re-verified by reading code (all of §2, §3
  table, §4 seam, §5 flows, §6, §8, §9 ports, §12 index; every node label in Diagrams
  a–e). Only F1 drifted.
- **Trap-checked the currency claims** with negative greps: `litellm`, `e2b`,
  `ANTHROPIC_BASE_URL`, `x-sandbox-token`, `/v1/llm/complete`, `packages/`,
  hierarchical-memory tokens → **all 0 / absent**, matching every DESIGNED/IN-FLIGHT
  marker. No aspirational-as-built slippage found.
- **Cross-checked the matrix** (`VALID_RUNTIMES`, `HARNESS_PROVIDERS`, both default
  maps, all four `supported_providers()`) against `runtime_matrix.py` + the harness
  classes → exact.
- **Boundary-checked the risky citations** (e.g. is `encrypt_message:3728` inside
  `send_fallback_message`? yes, fn spans 3609–3749; is `reporter.report_error` real?
  yes, `agent/status_reporter.py:185`, called `worker.py:1555`) → held up.
- **Census diff** of all 136 `.py` files against the doc's §2 → only `codex_session.py`
  (F2) is a load-bearing module the doc doesn't name.

No gating finding survived. The doc is accurate.

---

## Gating-rule application (rubric §Verdict)

| Gating trigger | Present? |
|---|---|
| Any M1 load-bearing subsystem omission | **No** (only a minor sibling *module*, codex_session) |
| M2 accuracy < ~0.85 | **No** (0.96) |
| Any M3 absent/wrong core flow | **No** (9/9 correct) |
| Any M4 wrong seam | **No** (5/5 right-altitude) |
| Any M6 mis-stated status | **No** (count 0) |

→ **Zero gating findings → verdict TRUSTWORTHY.** The three residual findings (1 MINOR,
2 NIT) are optional polish and do **not** trigger a revision loop; they may be folded
into any future edit at the author's discretion.

---

## Scope & boundary attestation

- This review is **read-only** w.r.t. `src/` and w.r.t. `docs/ARCHITECTURE.md`. The only
  repo change is the creation of this file (`docs/ARCHITECTURE-REVIEW.md`).
- Branch: `fleet/pyagent-arch-review`, stacked on base `fleet/pyagent-arch-doc`.
  `origin/main` is untouched and not pushed. Any PR is held on base
  `fleet/pyagent-arch-doc` for human review; **not self-merged.**
- No secrets/credentials appear in this review (verified: only key *paths* and public
  cert/verify symbol names are referenced, never key material).
