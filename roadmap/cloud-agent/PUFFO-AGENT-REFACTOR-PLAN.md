# puffo-agent thin refactor — Stage B plan

Stage A (this PR) turned `puffo-agent` into a **`uv` workspace of three members**
and severed the cloud runtime from the fat code:

- **`puffo-agent-core`** (`packages/puffo-agent-core/`) — stdlib-only shared kernel:
  `paths.py` (`home_dir`, `agents_dir`, `agent_dir`, `agent_yml_path`,
  `is_valid_agent_id`) and `profile.py` (`extract_soul_body`, `_soul_section_span`).
- **`puffo-agent-cloud`** (`packages/puffo-agent-cloud/`) — slim cloud runtime, moved
  out of `src/puffo_agent/agent/api_puffo/`. Thin deps only: `aiohttp`, `pyyaml`,
  `puffo-agent-core`. New: `config.py` (thin `CloudAgentConfig`), `cloud_http.py`
  (thin metadata HTTP client), `__main__.py` (standalone E2B entry point).
- **`puffo-agent`** (root) — the fat local agent. Re-exports the moved helpers from
  `puffo_agent_core` so existing call sites are unchanged; depends on both new members
  (`core ← cloud ← fat`, acyclic) and launches the cloud runner locally for api-puffo
  agents.

The slim-import gate is green: in an env with only `aiohttp` + `pyyaml` +
`puffo-agent-core` + `puffo-agent-cloud`, `python -c "import puffo_agent_cloud.__main__"`
exits 0 — no `pyside6` / `psutil` / fat `puffo_agent` / `anthropic` / `mcp` /
`cryptography`.

## Deferred to Stage B

1. **Promote the full agent config into `puffo-agent-core`.** Stage A's
   `CloudAgentConfig` reads only the fields `runner.py` uses (display_name, the LLM
   `runtime` triple, profile path). Promote the full `AgentConfig` + `RuntimeConfig` +
   `PuffoCoreConfig` + `TriggerRules` **and** `portal/runtime_matrix.py`
   (`migrate_legacy_kind`, `validate_triple`) into core, then have both the fat
   `portal.state` and the cloud `config.py` consume the one canonical loader instead of
   maintaining two parsers over the same `agent.yml` schema. Keeps the on-disk schema
   single-sourced.

2. **Wire `cloud_http.py` into the runner's live tool surface.** `CloudMetadataClient`
   (4 read-only routes: `/spaces`, `/spaces/{id}/channels`, `/spaces/{id}/members`,
   `/identities/profiles`) ships + is unit-tested in Stage A but is **not** yet exposed
   as agent tools. Stage B: add `list_members` / `list_profiles` (and possibly
   `list_channels`) tools to `tools.py`, decide whether the richer HTTP `/spaces`
   replaces the WS `list_spaces` round-trip, and thread a `CloudMetadataClient` through
   `ApiPuffoRunner` alongside the bridge.

3. **E2B packaging + network-rule token injection.** Build a wheel (or lockfile-pinned
   install) for `puffo-agent-cloud` + `puffo-agent-core` that drops into the E2B sandbox
   image, and wire the production `x-sandbox-token` path: today `cloud_http.py` sends the
   header only when a token is configured (explicit arg or `PUFFO_SANDBOX_TOKEN`),
   relying on edge network-rule injection in prod — confirm and document that contract
   end-to-end with the sandbox provisioner.

4. **Resolve the `websockets`-vs-`aiohttp` WS question.** The fat package still declares
   `websockets>=12.0`, but the cloud bridge **and** LLM client both ride `aiohttp` — the
   `websockets` library is not imported by the cloud runtime. Confirm nothing in the fat
   package still needs `websockets`; if not, drop it. The cloud package deliberately
   declares neither `websockets` nor `anthropic`.

5. **Recreate the missing design docs.** Two docs the task referenced are absent on this
   branch and were planned-from-code, not from them:
   `roadmap/cloud-agent/CLOUD-AGENT-PACKAGING-EVAL.md` and
   `PUFFO-AGENT-HTTP-CLIENT-FIX-for-ming.md`. Recreate (or locate + cross-link) them so
   the packaging rationale and the HTTP-client contract are written down.

6. **(Optional) Crypto core primitives.** If a later cloud feature needs any shared
   crypto helper, extract only the stdlib-safe primitive into `puffo-agent-core`; the
   fat `PuffoCoreHttpClient` / subkey signing / `KeyStore` / `mcp/puffo_core_server.py`
   stay in `puffo-agent` (the sandbox holds no key material by design).

## Notes / deviations carried from Stage A

- **Thin deps are `aiohttp` + `pyyaml`** (the task narrative said "aiohttp +
  websockets"). The cloud runtime uses `aiohttp` for both the WS bridge and the LLM
  HTTP client and `pyyaml` to read `agent.yml`; `websockets` is never imported.
- The fat package **depends on** `puffo-agent-cloud` (daemon/worker launch the cloud
  runner locally), refining the task's "fat depends on core + pyside6" to
  `core ← cloud ← fat`.
- `uv.lock` is committed at the root so `uv sync` is reproducible across the workspace.
