# puffo-agent performance audit — 2026-06-27

Snapshot of the 4-dimension audit run on `perf-audit-2026-06-27`.
This file tracks what got addressed in that branch and what we
chose to defer.

## Addressed in this branch

| # | Finding | Fix |
|---|---|---|
| 4 | `runtime.json` rewritten ~12×/min per agent (`portal/state.py:1221`) | Throttle: skip the disk write when only `updated_at` changed AND last write was <25s ago (CLI staleness gate is 30s). Cache keyed by resolved path to survive test `tmp_path` reuse. |
| 5 | Reconcile tick re-parses every `agent.yml` every 2s (`portal/daemon.py:235`) | `_load_agent_cfg_cached`: stat the file, return cached `AgentConfig` if (mtime_ns, size) unchanged. Cache evicted when agent disappears from disk. |
| 3 | `send_message` drops server-reported `missing_devices` (`mcp/puffo_core_tools.py:432-485, 1037-1095`) | Port the web client's post-send supplementation: capture POST response, re-fetch `/certs/sync` for the recipient slugs, build a same-`envelope_id` envelope wrapping the same `content_key` for the missing device_ids, fire-and-forget POST. Best-effort (the original send is already durable). Applied to `send_message` + `send_message_with_attachments`. |

Not addressed (scoped out per operator):

| # | Finding | Why deferred |
|---|---|---|
| 1 | Reconcile loop serially `await`s `worker.wait_warm` for each agent (`portal/daemon.py:233-273`) | Operator: "启动挺快的" (startup is fast enough in practice). Comment at line 256-259 documents the OOM-on-parallel-warm constraint; if startup latency becomes user-visible later, semaphore-bound parallelism is the lever. |
| 2 | `MessageStore.cleanup()` never wired (`agent/message_store.py:596`) | Local DBs total 3.9MB across 10 agents (largest 832KB). Not urgent. Revisit when an agent's DB exceeds ~100MB. |

## Deferred — review later

### Server access

- **`_fetch_user_profile` ignores disk cache on miss** (`agent/puffo_core_client.py:2098-2137`) — also `_resolve_space_name` (`:2340`), `_resolve_channel_name` (`:2354-2393`). Cold-start re-fetches names already on disk. Fix: consult `disk_cache.load_profile/load_space/load_channel` before HTTP.
- **`_resolve_space_name` fetches full `/spaces` per unknown space_id** (`agent/puffo_core_client.py:2340`) — and only caches one entry. Fix: populate every returned entry; share a single in-flight task to coalesce concurrent callers.
- **`_resolve_channel_name` event-replay per unknown channel** (`agent/puffo_core_client.py:2354-2393`) — `/spaces/X/channels` returns every name in one shot. Fix: try the channels endpoint first; fall back to the events scan only on empty.
- **`whoami` MCP tool fetches own profile every call** (`mcp/puffo_core_tools.py:351`) — Fix: route through `data_client`/profile cache.

### DB

- **`idx_messages_dm` doesn't cover the `recipient_slug` arm** of `get_dm_history WHERE (sender_slug=? OR recipient_slug=?)` (`agent/message_store.py:31-32, 333-344`). Full-scan on inbound DMs. Fix: add `idx_messages_dm_recipient ON messages (recipient_slug, sent_at) WHERE envelope_kind='dm'`.
- **Missing PRAGMAs** (`agent/message_store.py:130-131`) — default `synchronous=FULL` halves write latency vs `NORMAL` under WAL. Fix: add `synchronous=NORMAL`, `cache_size=-20000`, `temp_store=MEMORY`, `mmap_size=256MB`.
- **Double `channel_exists` round-trip** per `get_channel_roots` / `get_thread_messages` (`portal/data_service.py:230` + `agent/message_store.py:437`). Same `SELECT 1 … LIMIT 1` runs twice.

### Async parallelism

- **`_migrate_linked_agents_at_startup` serializes per-operator AND per-agent** (`portal/daemon.py:494-512` + `portal/control/link.py:179-238`). With N pairings × M agents = O(N·M) serial round-trips at startup. Pattern: `_warm_member_caches` (`agent/puffo_core_client.py:2189`) already does it right with `asyncio.gather`. Copy that.
- **`_save_inbound_attachments` fetches blobs serially per envelope** (`agent/puffo_core_client.py:3448-3505`) — 5 images = 5× sequential blob GET + 5× Pillow decode. Fix: `gather` over per-blob coroutines.
- **`list_channels_in_all_spaces` MCP tool serial per-space fetch** (`mcp/puffo_core_tools.py:700-720`). Fix: `gather`.

### Other

- **Sync file writes in per-turn hot path** — `AuditLog.write` (`agent/adapters/cli_session.py:128-143`), `current_turn.json` (`portal/worker.py:1182-1191`). Fix: `asyncio.to_thread` or `aiofiles`.
- **Per-call `aiohttp.ClientSession()` in `_me_loop`** (`portal/control/client.py:406`) — new session every 30s for one POST. Fix: hold one session on `ControlManager`.
- **macOS `claude_has_credentials()` spawns 2 `security` subprocesses every 30s** (`agent/cli_bin.py:271-291` × `HEARTBEAT_INTERVAL_SECONDS = 30s`) — ~5760 spawns/day. Already off-thread so loop isn't blocked, but it's pure waste. Fix: 5-min TTL memoize, invalidate on refresh-success.

## Already correct (audited, not red flags)

- WS heartbeat: server-initiated ping/pong, no client keepalive timer (`crypto/ws_client.py:133`)
- WS reconnect: exponential backoff 1→30s (`crypto/ws_client.py:176-200`)
- `_invite_poll_loop`: 10/30s adaptive + 5min age-gate (`agent/puffo_core_client.py:1388`)
- `DeviceKeyCache` for INBOUND decrypt is properly cached + invalidated (`agent/puffo_core_client.py:385`)
- `CredentialRefresher._refresh_now` lock is PUF-221 single-writer (don't touch) (`portal/credential_refresh.py:935-966`)
- `_consume_queue` serial dispatch is per-thread ordering invariant (`agent/puffo_core_client.py:1246`)
- `handle_envelope` decrypt-before-admit is correctness-required (`agent/puffo_core_client.py:603-902`)
- `_stop_all_workers` already uses `asyncio.gather` (`portal/daemon.py:375-377`) — the reconcile-tick stop fan-outs should follow this when the parallelism question is revisited
