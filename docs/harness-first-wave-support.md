# Pi, OpenCode, and ACP first-wave support

This matrix describes the protocol surface implemented by the Puffo Agent.
It is deliberately narrower than a claim that every upstream binary or model
provider combination has been exercised live.

## Runtime selection and startup

| Harness | Puffo-facing transport | Startup command | Binary ownership | Admission boundary |
| --- | --- | --- | --- | --- |
| Pi | Persistent JSONL RPC over stdin/stdout | `pi --mode rpc` | The machine daemon resolves `pi`, including `PUFFO_PI_BIN` and the reconstructed login-shell `PATH`. | `PiDriver.open()` fails closed unless the shipped Puffo tool bridge loads and attests its nonce. |
| OpenCode | One `run --format json` child per Puffo turn | `opencode run --format json ...` | The machine daemon resolves `opencode`, including `PUFFO_OPENCODE_BIN`. | OpenCode must emit the pinned JSON event shapes; unknown or malformed frames remain visible as runtime warnings/failures. |
| Generic ACP | Persistent ACP v1 NDJSON over stdin/stdout | Operator-supplied argv, for example `opencode acp` | The argv is explicit because ACP is a protocol, not one executable. | The ACP SDK negotiates protocol version 1 during `initialize`; startup fails if negotiation or session creation/loading fails. |

OpenCode's ACP command is not a network transport from Puffo's perspective.
OpenCode may start an internal HTTP server for its own SDK backend, but
`opencode acp` reads and writes ACP NDJSON on stdin/stdout. On 2026-08-25,
OpenCode 1.18.16 returned a valid ACP v1 `initialize` response both with and
without `--pure` in a local admission probe.

## Negotiated behavior

| Capability | Pi RPC | OpenCode JSON mode | Generic ACP v1 |
| --- | --- | --- | --- |
| Model selection | `--model <model>` when configured | Provider-qualified `<provider>/<model>` | Known executable presets (currently Gemini and Kimi) inject their native `-m`; unknown targets run their default model and emit an explicit diagnostic instead of silently claiming selection. |
| Session resume | `switch_session` on the persistent child | `--session` on the next per-turn child | Advertised only when `loadSession` is negotiated. |
| Cancel | Typed Pi `abort` command | Process termination for the active one-shot child | Typed ACP cancellation. |
| Steer while busy | Available only when the Pi bridge and runtime state permit it | Not supported | Not claimed by the base ACP v1 profile. |
| Context/usage | Pi message usage and compaction events | Usage from OpenCode step-finish events | Context occupancy may be pushed; the base ACP v1 profile makes no token-usage claim. |
| Compact | Typed Pi compaction command/events | Not supported | Not claimed by the base ACP v1 profile. |
| Permissions/tools | Puffo's attested Pi extension bridge | OpenCode owns its tool policy; Puffo does not claim a permission bridge | ACP permission requests are bridged through the typed client callback. |
| Lifecycle | Persistent child | Per-turn child | Persistent child |

ACP prompt failures preserve a bounded diagnostic and pass structured auth
errors or free-text provider failures through Puffo's shared failure classifier.
This is an error-normalization profile, not a private ACP wire extension.

The Web model picker intentionally does not borrow Codex's model catalog or
inference-level list for Pi, OpenCode, or generic ACP. Until a selected
harness reports a negotiated catalog, the UI offers only an explicit custom
model value and omits the inference selector.

## Verification evidence and limits

The repository regression suite covers binary resolution, launch argv,
bridge attestation, protocol normalization, turn completion, session resume,
cancellation, child exit, and restart-facing runtime contracts with pinned
fixtures and fake child processes. The focused first-wave suite is:

```text
tests/test_cli_bin.py
tests/test_local_runtime_migration.py
tests/test_pi_driver.py
tests/test_pi_tool_bridge.py
tests/test_opencode_driver.py
tests/test_opencode_protocol.py
tests/test_acp_driver.py
tests/test_acp_sdk_conformance.py
tests/test_runtime_exited_contract.py
```

The OpenCode ACP admission probe above is live executable evidence. Pi was
not installed on the verification host, and no external model credentials
were used, so this document does not claim a live provider-backed Pi turn or
a provider-backed install-to-restart journey. Those remain release-environment
acceptance checks rather than facts inferred from fixture tests.
