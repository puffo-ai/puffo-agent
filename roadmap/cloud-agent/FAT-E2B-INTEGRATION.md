# FAT `puffo-agent` → E2B cloud drop-in

Notes for making the FAT `puffo-agent` a clean drop-in for the thin
`puffo-agent-cloud` in the E2B/cloud image. Two independent changes
landed here: **slim packaging** (so `pip install puffo-agent` is Qt-free)
and a **config-driven LLM base URL** (so cloud agents' model calls route
through Shan's LiteLLM virtual-key endpoint instead of the vendor
default). Neither changes native/desktop behavior when the new config is
absent.

> **Keyless outbound dispatch (T23 phase 1):** how a keyless cloud agent
> does all outbound tool work over unsigned `/v2/cloud-agents/*` HTTP
> (egress-injected `x-sandbox-token`) instead of the bridge WS — and which
> GAP rows flip to PASS — is written up in
> [`KEYLESS-DISPATCH-PHASE1.md`](./KEYLESS-DISPATCH-PHASE1.md).

---

## 1. Slim packaging — base install is Qt-free

`pip install puffo-agent` no longer pulls **PySide6** (Qt). The only
GUI-only dependency was `pyside6`; it moved out of `[project].dependencies`
into a new `[project.optional-dependencies] gui` extra:

```toml
[project.optional-dependencies]
gui = ["pyside6>=6.7"]
```

Consequences for the cloud image:

- **Base install runs headless.** `puffo-agent start` (→ `run_daemon`),
  `puffo-agent --help`, and `import puffo_agent.portal.daemon` all import
  and run with PySide6 **absent**. Every `import PySide6` in the source
  lives under `src/puffo_agent/portal/ui/`, and those modules are only
  reached from the UI entry points — never from the daemon path.
- **Restore the desktop UI** with `pip install 'puffo-agent[gui]'`. Then
  `start --ui` (desktop window) and `start --tray` (menu-bar) work as
  before.
- **GUI commands without the extra fail loud, not ugly.** `start --ui` /
  `start --tray-runner` invoked without `[gui]` print an actionable hint
  (`pip install 'puffo-agent[gui]'`) and exit non-zero, instead of a raw
  `ModuleNotFoundError` traceback. See `portal/cli.py cmd_start`.

The cloud/E2B image should install the **base** package (no `[gui]`), which
keeps the image slim and Qt-free. Cloud agents use `cli-local`; no provider
Python SDK extra is installed.

> `requirements.txt` is a stale partial list that never listed pyside6 —
> `pyproject.toml` is the source of truth for dependencies.

---

## 2. LLM plane — route model calls through the LiteLLM VK

Cloud agents should send their model calls to Shan's LiteLLM **virtual
key (VK)** endpoint (an OpenAI/Anthropic-compatible base URL) rather than
`api.anthropic.com` / `api.openai.com`. This is driven entirely from the
`runtime` block of each agent's `agent.yml` — **no hard-coded URLs and no
embedded key** in the package.

### Fields Shan's cloud bundle sets (per agent `agent.yml`)

```yaml
runtime:
  kind: cli-local
  harness: claude-code        # or codex for provider=openai
  llm_base_url: "<VK endpoint>"   # e.g. the LiteLLM proxy base URL
  api_key: "<VK>"                 # the virtual key (reuses the existing api_key field)
  # ...model / provider / etc. as usual
```

- **`llm_base_url`** — new field. OpenAI/Anthropic-compatible base URL.
  Empty/absent → the vendor endpoint, i.e. **today's behavior, byte-for-
  byte unchanged**.
- **`api_key`** — the existing field, reused to carry the VK secret. No
  new secret field was introduced.

### What consumes them, per `runtime.kind`

| `runtime.kind`            | `llm_base_url` routing                                                                 | VK secret (`api_key`)                          |
| ------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `cli-local` / `claude-code` | `ClaudeCodeDriver` receives `ANTHROPIC_BASE_URL` in its process environment | `api_key` → `ANTHROPIC_API_KEY`, only when `llm_base_url` is set |
| `cli-local` / `codex`       | `CodexDriver` receives a generated model-provider entry pointing at the gateway | `api_key` is written only to the isolated generated provider config |

Notes:

- **`cli-local` with an empty `llm_base_url` injects nothing** into the
  spawn env — claude keeps its `claude login` / OAuth credential path
  (`~/.claude/.credentials.json`). Only when `llm_base_url` is set do we
  add `ANTHROPIC_BASE_URL`, and only then (and when `api_key` is
  non-empty) do we add `ANTHROPIC_API_KEY=<VK>` so the CLI authenticates
  against the VK.
- The Claude env mapping is a single shared pure helper,
  `puffo_agent.agent.adapters.base.anthropic_base_url_env(base_url)`,
  used by the local Driver preparer and unit-tested without importing a vendor
  Python SDK.

### Follow-ups (out of scope for this change)

- **`cli-docker`** base-URL routing — threading `ANTHROPIC_BASE_URL` (and
  the VK) into the per-agent container env is not wired yet.
- **Provisioning / rotating the VK** is Shan's; this change only threads
  the config-driven fields. No key is embedded, defaulted, or fetched by
  the package.

---

## Verification snapshot

- `pip install .` in a fresh venv → `import PySide6` raises
  `ModuleNotFoundError`, while `import puffo_agent.portal.daemon` and
  `puffo-agent --help` succeed. `pip install '.[gui]'` →
  `import puffo_agent.portal.ui.launcher` succeeds.
- `tests/test_slim_packaging_headless.py` — headless import + GUI-hint guard.
- `tests/test_llm_base_url_routing.py` — Claude and Codex Driver gateway
  routing, config round-trip, the shared env helper, and the inert empty-base
  case.
