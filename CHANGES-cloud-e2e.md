# Cloud-E2E thin-agent changes (`feat/cloud-e2e-thin`)

Branch off `origin/fleet/puffo-agent-thin-refactor`. **Never merged to puffo-agent
`main`/`dev` or the fleet branch's canonical state** — it exists only so the
cloud-agent E2E (cloud-infra `docs/integration-board.md`) can pin its E2B template
build (`build.sh` auto-pin) to a ref where the thin runtime completes the messaging
round-trip. The **full/local agent is untouched** — every change is inside
`packages/puffo-agent-cloud`.

Owner: S3-messaging. Each change is listed with what + why.

---

## Workstream A — direct-to-LiteLLM inference + MCP-only reply

Supersedes the earlier auto-reply-text experiment (commit `2d8422f`): the runtime
now talks **directly** to the LiteLLM gateway and replies **only** by calling the
`send_message` MCP tool. Files: `cloud_client.py`, `bundle.py`, `config.py`,
`runner.py`, `tests/test_api_puffo_runner.py`.

### A1 — `CloudLlmClient.complete()` hits the gateway directly
`cloud_client.py`. The client is now constructed with `(gateway_url, api_key)` and
calls **`{gateway_url}/v1/messages`** (LiteLLM's Anthropic-compatible endpoint)
instead of puffo-server's `/v1/llm/complete`.
- **Auth:** dropped `Authorization: Bearer <sandbox_token>`; now sends
  `x-api-key: <api_key>` + `anthropic-version: 2023-06-01`.
- **Body:** native Anthropic shape — `system` (from `system_prompt`), `messages`,
  `tools`, `model`, and an injected `max_tokens` (default 1024). `provider` and
  `api_key` are **no longer in the body**.
- Response parsing unchanged (already Anthropic `content[]` / `stop_reason` /
  `tool_use`).
- **Why:** the agent's inference no longer needs the puffo-server proxy hop; it
  authenticates to LiteLLM with its own per-agent virtual key. Simpler, and keeps
  the LLM plane independent of the bridge plane.

### A2 — `litellm_gateway_url` bundle field
`bundle.py` + `config.py`. New bundle field `litellm_gateway_url`, written into
`agent.yml`'s `runtime` block and loaded onto `CloudRuntime`; `runner.py`
constructs the LLM client from `runtime.litellm_gateway_url` + `runtime.api_key`
(the existing `api_key` **is** the LiteLLM virtual key). Optional in `from_dict`
(default `""`) so legacy bundles still ingest during rollout; the runner logs a
clear warning when it is unset.
- **⚠ Coordination:** AIM's create-bundle (S2-CRUD-template
  `lifecycle._build_install_bundle`) must now populate `litellm_gateway_url` (the
  public/tunnelled LiteLLM gateway the sandbox can reach) alongside the `api_key`
  virtual key. Until it does, the LLM plane is inert (agent still boots + connects).

### A3 — MCP-only reply
`runner.py`. Removed the `2d8422f` auto-reply text shim. `_run_turn` no longer
returns `(reply, posted)` and never auto-posts text. The reply target (DM
`sender_slug`, or the originating `space_id`/`channel_id`) is threaded into the
turn, and the **system prompt** now instructs the model: *to reply you MUST call
`send_message`* (with `recipient_slug=<sender>` for a DM, or the channel ids).
`tools.py::send_message` → `bridge.send_send` is the sole reply path.
- **Why:** make the reply an explicit MCP action (auditable, routable, matches the
  fat-agent tool model) rather than an implicit text fallback. A plain-text answer
  is intentionally **not** delivered.

### Tests
`tests/test_api_puffo_runner.py`:
- E2E test updated: mock LLM served at `/v1/messages`; asserts the direct-gateway
  Anthropic body (`system`, `max_tokens=1024`, no `provider`/`api_key`/
  `system_prompt`), the `x-api-key` + `anthropic-version` headers, and that the
  system prompt tells the model to `send_message` the DM sender.
- New `test_runner_mcp_only_text_reply_not_delivered`: a text-only answer delivers
  **nothing** (guards the shim removal).
- 23 cloud-package tests pass; ruff clean on all changed files.

### Proven live
Ran the modified runtime locally against the standing puffo-server (bridge) + the
LocalStack **LiteLLM** gateway (direct, `x-api-key` = master key), agent + sender
seeded `--identity-kind human`: DM → `turn (sender=…)` → 2 LLM rounds
(`send_message` tool_use then `end_turn`) → **sender observed the reply `"4"`**.
Runtime log confirms `llm_gateway=http://localhost:<port>` (direct, not via
puffo-server).
