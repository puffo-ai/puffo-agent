# Puffo Agent Codebase Map

This is a navigation guide for the current Python repository. Use
[`ARCHITECTURE.md`](ARCHITECTURE.md) for behavioral contracts and
[`FAT-CLOUD-ARCHITECTURE.md`](FAT-CLOUD-ARCHITECTURE.md) for the keyless cloud
transport delta.

`puffo-agent-architecture.drawio` is an older editable visual snapshot. It is
kept as source material, but the Mermaid diagrams in the two canonical
documents above describe the current implementation.

## Package Shape

The PyPI package is `puffo-agent`, the Python package is `puffo_agent`, and the
console entry point is `puffo_agent.portal.cli:main`.

| Package area | Responsibility |
| --- | --- |
| `portal/` | CLI, daemon, per-Agent worker lifecycle, config/state, machine control, loopback services, desktop UI, import/export. |
| `agent/` | Transport facade, durable message/Inbox state, Global Inbox orchestration, model context, sends, reminders, memory, and Runtime events. |
| `agent/harness/` | Provider-neutral Driver protocol, Runtime Manager, Codex app-server Driver, Claude stream-json Driver, and local/Docker runtime preparation. |
| `agent/adapters/` | Compatibility Adapter facade consumed by the common Inbox runtime. |
| `crypto/` | Native signed HTTP, encrypted WS, identity keys, message envelopes, and attachment crypto. |
| `mcp/` | Model-facing Puffo, Inbox/history, membership, memory, reminder, host, and file tools. |
| `macos/` | Host credential integration. |
| `tests/` | Contract, regression, integration, packaging, and UI-helper coverage. |

## Composition Roots

### Daemon

Start at `portal/daemon.py` for process ownership. The daemon:

1. loads `daemon.yml` and control state;
2. starts data, RPC, WS-local, and remote-control services;
3. scans `agents/<agent-id>/agent.yml`;
4. creates, restarts, pauses, archives, or removes workers to match desired
   state;
5. owns shutdown and process-level diagnostics.

Management commands mutate desired state or write a lifecycle flag. They do not
reach into a running Driver directly.

### Worker

`portal/worker_run.py:StandardWorkerRun` is the non-WS-local composition root.
It prepares paths and prompts, builds transport and runtime state, warms them,
constructs the Global Inbox and send coordinator, starts background services,
listens, and cleans up.

The central assembly is `_build_global_runtime()`:

```text
MessageStore
  + provider Adapter/Driver
  + ContextController
  + BaselineAdapter / ActiveBoundaryAdapter
  + HeldRecoverySource
  + SendCoordinator
  + ReminderScheduler
  -> GlobalInboxRuntime
```

`portal/ws_local/` is a separate runtime attachment path. It keeps the daemon as
transport/tool owner while an external process supplies the Agent brain.

## Message Subsystem

| Concern | Owner |
| --- | --- |
| Native receive | `agent/inbound_receipts.py` and native helpers behind `PuffoCoreMessageClient`. |
| Keyless receive | `agent/bridge_client.py`, `agent/bridge_transport.py`. |
| Shared ingress policy | `agent/ingress_policy.py`, `agent/dm_gate.py`, membership modules. |
| Persistence and frontiers | `agent/message_store.py`, `agent/receipt_persistence.py`, `agent/inbox_store.py`. |
| Route/thread normalization | `agent/thread_context.py`, `agent/message_store_models.py`. |
| Model-readable context | `agent/message_projection.py`, `agent/prior_context.py`, `agent/message_context.py`. |
| Global scheduling | `agent/inbox_scheduler.py`, `agent/global_inbox_runtime.py`. |
| Context admission | `agent/context_controller.py`, `agent/global_inbox_admission.py`. |
| Held recovery | `agent/global_inbox_held.py`, `agent/held_context.py`. |
| Outbound coordination | `agent/send_coordinator.py`, `agent/global_inbox_send.py`, `agent/outbound_messages.py`. |

`PuffoCoreMessageClient` is a compatibility facade and state owner, not the place to
add another policy. Put transport-specific mechanics in native/bridge modules
and shared behavior in the leaf policy/projection/store modules.

## Runtime Subsystem

`agent/harness/driver.py` defines the common command and event vocabulary.
`RuntimeManager` owns one active runtime/session/turn state machine and enforces
capability checks and terminal transitions.

Current concrete engines:

- `agent/harness/drivers/codex.py`: Codex app-server.
- `agent/harness/drivers/claude_code.py`: Claude Code stream-json CLI.
- `agent/harness/runtime/docker_runtime.py`: per-Agent Docker placement and mounts for both Drivers.
- `agent/harness/runtime/docker_support.py`: pinned image and bounded Docker lifecycle helpers.
- `portal/ws_local/`: externally attached runtime.

`agent/harness/runtime/local_runtime.py` resolves host binaries, isolated homes,
durable native-session resume, managed config, and common Driver binding. Docker reuses
the same Driver and Runtime Manager contracts through a `docker exec -i`
process factory. `portal/runtime_matrix.py` is the source of truth for supported
runtime/provider/harness combinations.

## MCP Subsystem

`mcp/puffo_core_server.py` is the stdio MCP entry point. Registration is split
by capability under `mcp/core_*_tools.py`; the older
`mcp/puffo_core_tools.py` remains a compatibility facade and shared helper
owner.

Read tools route through the daemon data service or in-process data client and
return the common message projection. Send tools call the daemon-owned
`SendCoordinator`; the model never supplies sequence or baseline fields.

Memory tools are split between `mcp/memory_tools.py` and
`mcp/memory_tool_registration.py`. Reminder lifecycle tools are registered from
`mcp/lifecycle_tools.py` and `mcp/lifecycle_tool_registration.py`.

## Durable State

```text
~/.puffo-agent/
  daemon.yml
  control/
  shared/
  agents/<agent-id>/
    agent.yml
    profile.md
    keys/
    messages.db
    runtime_events.db
    runtime.json
    memory/
    workspace/
      shared -> ../../../shared/
```

| Artifact | Owner |
| --- | --- |
| `agent.yml` | Desired Agent, runtime, and Puffo transport configuration. |
| `profile.md` | Operator-editable source persona; synchronized into managed briefing content. |
| `messages.db` | Messages, receipt decisions, Inbox/turn state, frontiers, and reminders. |
| `runtime_events.db` | Bounded local outbox and resumable local Driver session references. |
| `runtime.json` | Daemon health and status projection for CLI/UI. |
| `memory/` | Bounded briefing, notes, recollection, imports, and Git history. |
| `workspace/shared/` | Managed link to the Puffo-home cross-Agent collaboration directory. |
| `workspace/.puffo/inbox/` | Materialized inbound attachments. |

Native Agents use `keys/`. Keyless bridge Agents use a scoped token and do not
need local Puffo message keys.

## Control And Services

| Area | Start here |
| --- | --- |
| Machine link and pairings | `portal/control/link.py`, `portal/control/store.py`. |
| Authenticated control WS | `portal/control/client.py`, `portal/control/envelope.py`. |
| Remote Agent mutation | `portal/control/provision.py`, `portal/control/agent_message.py`. |
| Data service (`63386`) | `portal/data_service.py`. |
| RPC service (`63385`) | `portal/rpc_service.py`. |
| WS-local (`63387`) | `portal/ws_local/server.py`, `portal/ws_local/session.py`. |
| Desktop UI | `portal/ui/`. |

## Safe Change Boundaries

- A wire field change must be traced through Server producer, native/bridge
  parser, persisted model, projection, MCP serialization, and tests.
- A message behavior rule belongs once in shared ingress, context, or send
  policy, not once per transport.
- Provider-specific commands and frames stop at a Driver.
- Durable state changes need explicit migration and restart behavior.
- Prompt and model-facing context changes must preserve target, sender, time,
  sequence, thread, visibility, and truncation semantics.
- Keep tests around protocol invariants and reproduced regressions; avoid
  duplicating the same behavior across every facade.
