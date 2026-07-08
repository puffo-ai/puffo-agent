# puffo-agent Codebase Architecture

This document summarizes the current architecture of `puffo-agent` as read from
the source tree. The companion diagram is
[`puffo-agent-architecture.drawio`](puffo-agent-architecture.drawio).

## Scope

`puffo-agent` is a local Python daemon that supervises many Puffo bot accounts
on one machine. Each agent has isolated on-disk state, an optional local or
containerized LLM runtime, Puffo Core credentials, message storage, and local
workspace files.

The package is distributed as `puffo-agent`; the Python module is
`puffo_agent`. The console entry point is:

- `puffo-agent = puffo_agent.portal.cli:main`

## Top-Level Shape

| Area | Main modules | Responsibility |
| --- | --- | --- |
| CLI and daemon | `portal/cli.py`, `portal/daemon.py`, `portal/state.py` | Start/stop/status commands, read and write on-disk config, reconcile desired agent state into running workers. |
| Worker runtime | `portal/worker.py`, `agent/core.py`, `agent/adapters/*`, `agent/harness/*`, `agent/providers/*` | Run one agent loop, build system prompt, dispatch messages to the selected runtime, record runtime state. |
| Puffo protocol | `crypto/*`, `agent/message_store.py`, `agent/status_reporter.py` | Signed HTTP, WebSocket relay, HPKE/AEAD message encryption, local encrypted message history, status reporting. |
| Local bridge | `portal/api/*` | Loopback HTTP API for local web clients to pair, manage agents, inspect files/logs, import/export, and attach WS-local tools. |
| Remote control | `portal/control/*` | Machine linking, operator pairings, encrypted control WebSocket, remote create/edit/pause/resume/archive/refresh commands. |
| Local services | `portal/data_service.py`, `portal/rpc_service.py`, `portal/ws_local/*` | Loopback data/RPC APIs and WS-local attachment protocol used by MCP and external tools. |
| MCP tools | `mcp/*` | Stdio MCP server exposing Puffo messaging tools plus host-side skill/MCP/refresh tools to CLI runtimes. |
| Desktop UI | `portal/ui/*` | PySide6 desktop window, tray runner, agent/operator/log/workspace views. |
| Tests | `tests/*` | Pytest suite covering crypto, bridge, control, worker/runtime, MCP, WS-local, UI helpers, and packaging behavior. |

## Runtime Flow

1. The operator starts `puffo-agent start`.
2. `portal.daemon.Daemon` loads `DaemonConfig`, starts auxiliary loopback
   services, starts credential refresh loops, starts the remote control manager,
   then repeatedly reconciles `~/.puffo-agent/agents/`.
3. Each discovered `agent.yml` becomes a `portal.worker.Worker` unless the agent
   is paused, deleted, archived, or invalid.
4. The worker builds an adapter from `runtime.kind`:
   - `chat-local`: direct provider-backed chat adapter.
   - `sdk-local`: Claude SDK adapter.
   - `cli-local`: local CLI process with isolated per-agent home.
   - `cli-docker`: Dockerized CLI process with mounted agent state.
   - `ws-local`: attached local WebSocket tool session.
5. The worker owns the Puffo Core listen/send loop, persists `runtime.json`,
   writes managed prompt files, and calls `agent.core.PuffoAgent` for each turn.
6. `PuffoAgent` converts Puffo messages into adapter `TurnContext`, tracks a
   conversation log and memory, then routes replies either through explicit MCP
   `send_message` calls or fallback assistant output.

The important design point is that the daemon is the reconciler and single
owner of worker lifecycles. Most management actions write `agent.yml` or a
sentinel flag; the next reconcile tick applies the state transition.

## On-Disk Contract

The filesystem is the internal control plane. By default state is under
`~/.puffo-agent/`, with `PUFFO_AGENT_HOME` as an override.

```text
~/.puffo-agent/
  daemon.yml
  daemon.pid
  control/
  shared/
  agents/<agent-id>/
    agent.yml
    profile.md
    memory/
    keys/
    messages.db
    runtime.json
    workspace/.puffo/inbox/
  archived/
```

Key conventions:

- `agent.yml` is the desired state for an agent.
- `runtime.json` is daemon-managed live state for CLI/UI visibility.
- `archive.flag`, `delete.flag`, `restart.flag`, and refresh flags request
  lifecycle changes.
- Per-agent virtual homes isolate `.claude`, `.codex`, sessions, credentials,
  and workspace-level prompt/config files.

## Local Bridge

`portal/api/server.py` builds an aiohttp app bound to loopback. It exposes:

- Discovery and pairing: `/v1/info`, `/v1/providers`, `/v1/pair`,
  `/v1/pairing`.
- Agent management: list/create/get/delete, profile/runtime update,
  pause/resume/restart/archive.
- Operator support: logs, workspace file browse/read, import/export,
  revoke-pending.
- WS-local: `GET /v1/ws-local`.

The bridge is intentionally not load-bearing for agent message delivery. If it
cannot bind its port, the daemon can continue running workers.

## Remote Control Plane

`portal/control` links a local machine to one or more remote operators:

- `store.py` persists the machine identity and operator pairings.
- `link.py` registers the machine, mints a link code, and migrates owned local
  agents after approval.
- `machine_auth.py` signs machine HTTP and WebSocket frames.
- `envelope.py` verifies and decrypts operator command envelopes.
- `client.py` maintains the machine control WebSocket and executes commands.

Remote control commands deliberately reuse the same local state model as the
bridge. For example, pause/resume updates `agent.yml`, archive touches
`archive.flag`, and refresh writes workspace refresh flags. This keeps one
execution path: the daemon reconcile loop.

## Puffo Core And Crypto

The `crypto` package implements the wire-protocol primitives used by the agent
runtime and MCP tools:

- `http_auth.py` signs and verifies HTTP requests.
- `http_client.py` wraps signed Puffo Core HTTP calls and rotates subkeys.
- `ws_client.py` maintains the Puffo Core WebSocket relay.
- `message.py` encrypts/decrypts message envelopes with content-key wrapping.
- `attachments.py` handles encrypted blob upload/download paths.
- `keystore.py`, `certs.py`, `primitives.py`, `canonical.py`, and `v2_aad.py`
  hold identity/session keys, certificate helpers, canonical signing bytes,
  HPKE/AEAD primitives, and AAD construction.

Message encryption separates:

- Inner signed payload bytes.
- Outer content ciphertext bound to envelope metadata through AAD.
- Per-recipient HPKE-wrapped content keys.
- Supplementation envelopes for newly discovered recipient devices.

## MCP And Host Tooling

`mcp/puffo_core_server.py` is the stdio MCP entry point used by CLI runtimes. It
combines:

- Puffo Core messaging tools from `mcp/puffo_core_tools.py`.
- Local host tools from `mcp/host_tools.py` for skill and MCP management.
- Data reads through `mcp/data_client.py` against the daemon data service.
- Optional daemon-mediated RPC through `mcp/_host_mcp.py`.

Adapter setup in `portal/worker.py` injects the correct environment for local
or Docker runtimes. Docker runtimes use host aliases for loopback services,
while local CLI runtimes use `127.0.0.1`.

## WS-Local

`portal/ws_local` supports externally attached local tools that do not run
inside the daemon's normal adapter process:

- `protocol.py` defines strict JSON frames such as `connect`, `bundle`,
  `ack`, `end`, `tool_call`, and `tool_result`.
- `hub.py` tracks attach points.
- `route.py` exposes the bridge WebSocket route.
- `session.py` runs a turn session over an abstract transport.
- `tool_dispatch.py` exposes the allowed Puffo tools.
- `ws_local_client.py` is the attach-side client entry path.

The daemon handles Puffo decryption/encryption; the attached tool sees
plaintext bundles and returns tool results or turn completion frames.

## Testing Map

The repository has a broad pytest suite. Useful clusters:

- Bridge and local API: `test_bridge_*`, `test_log_endpoint.py`,
  `test_pause_resume_archive.py`.
- Remote control: `test_control_client.py`, `test_control_envelope.py`,
  `test_link_migrate_soul_sync.py`.
- Worker/runtime/adapters: `test_worker_*`, `test_runtime_matrix.py`,
  `test_cli_session_recovery.py`, `test_codex_*`, `test_harness.py`.
- Puffo crypto/protocol: `test_crypto_primitives.py`, `test_message.py`,
  `test_http_client.py`, `test_ws_client.py`, `test_keystore_and_certs.py`.
- MCP/data/RPC: `test_puffo_core_tools.py`, `test_puffo_core_server_lifespan.py`,
  `test_data_service.py`, `test_rpc_service.py`, `test_host_mcp_handler.py`.
- WS-local: `test_ws_local_*`.
- UI helpers: `test_ui_mcp_probe.py`, `test_ui_mcp_scanner.py`,
  `test_ui_views.py`.

## Architectural Invariants

- Agent lifecycle is file-driven and reconciled by the daemon.
- Worker processes/tasks are per-agent; agent state is isolated on disk.
- The bridge and control plane mutate desired state, not running workers
  directly.
- Puffo Core traffic is signed, encrypted where required, and uses per-agent
  keystore material.
- CLI runtimes receive Puffo capabilities through generated MCP config and
  per-agent prompt/config files.
- Loopback services are local integration surfaces, not public network APIs.
