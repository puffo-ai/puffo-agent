---
name: evolve-puffo-runtime-contracts
description: Design or change Puffo Driver, MCP, WS-local, Inbox, context-control, and send-coordination interfaces without accidental surface area or producer-consumer drift. Use when adding methods, fields, events, callbacks, capabilities, compatibility behavior, or test probes across runtime boundaries.
---

# Evolve Puffo Runtime Contracts

Map the full contract before editing: producer, wire or persistence shape,
normalization boundary, consumer, error path, retry path, and observability.

## Admit New Surface Carefully

Add an API only when it is at least one of:

1. A real control primitive that drives the production path.
2. A stable cross-component contract.
3. A reusable derivation that combines multiple state sources.
4. A provider-native capability that a Driver must normalize.

Do not add a forwarding method, test-only state probe, or wrapper around one
field. Tests may inspect a local object directly when no production boundary
exists.

## Preserve Ownership

- Driver: provider invocation, native protocol, session lifecycle, event and
  capability normalization.
- Worker: construction, lifecycle, and wiring.
- Global Inbox runtime: one active planning/turn lifecycle.
- MessageStore: durable local message and turn transitions.
- SendCoordinator: freshness validation and semantic send outcomes.
- MCP tools: agent-facing semantic operations, not internal state access.

Required fields are accessed directly after normalization. Put legacy aliases,
optional plugin methods, and version compatibility in one named boundary; do
not scatter `getattr` fallbacks through consumers.

## Verify The Chain

Update and check every producer, serializer, persisted representation,
transport, consumer, fallback, and log field. Add only the smallest contract
test that would catch one side changing without the other.
