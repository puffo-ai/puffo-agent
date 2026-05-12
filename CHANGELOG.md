# Changelog

All notable changes to `puffo-agent` are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Auto self-introduction after accepting a channel invite. When the
  agent accepts an ``invite_to_channel`` (whether auto-accepted from
  the operator or accepted via the operator's ``y`` DM reply), the
  daemon enqueues a synthetic ``[puffo-agent system message]``
  envelope on the new channel asking the agent to post a short intro
  (default English, 2-3 sentences) using its normal
  ``mcp__puffo__send_message`` path. Existing personality from
  ``profile.md`` shapes the wording. A new sqlite table
  ``channel_intro_prompted(channel_id PK, prompted_at)`` (per-agent
  ``messages.db``) gates the nudge so a daemon restart or a
  server-side invite redelivery can't fire a second intro. Space-only
  invites and public channels the agent gains via space-membership
  fan-out are intentionally not nudged — the trigger is specifically
  the channel-level invite accept.

## [0.7.4] — 2026-05-12

### Fixed

- cli-local: MCP server was crashing on agent startup with
  `ModuleNotFoundError: No module named 'mcp'` on macOS / Linux
  installs where `mcp` lives in real-user user-site (e.g. installed
  via `pip install --user` or as a transitive dep of `pip install
  -e .`). The adapter rewrites `HOME` (and `USERPROFILE`) on the
  claude subprocess so each agent has its own `~/.claude`; the MCP
  subprocess claude spawns inherited that HOME, and Python computed
  user-site relative to it (`<per-agent-home>/Library/Python/...`),
  landing on an empty per-agent directory. Result: every
  `mcp__puffo__*` tool went missing and the agent fell back to
  plain-text replies. Now `puffo_core_mcp_env` injects
  `PYTHONUSERBASE` pointing at the daemon's real user base
  (captured via `site.getuserbase()` at module load time, before
  any HOME mutation), which the spawned MCP subprocess uses to
  resolve user-site independent of HOME — `.pth` files processed,
  editable installs honoured. cli-docker is excluded (container
  has its own Python tree with deps baked in, so a host path would
  be meaningless inside it); Windows is unaffected in practice
  because Windows user-site reads `APPDATA`, not `HOME`, but the
  `PYTHONUSERBASE` injection is harmless there.

## [0.7.3] — 2026-05-11

### Added

- New MCP tool `mcp__puffo__get_thread_history(root_id, limit,
  since, before, after)` — root post + every reply in one thread,
  oldest-first. Companion to the restructured
  `get_channel_history`; agents drill into a thread only when
  `get_channel_history` shows a non-zero reply count worth reading.
- `[puffo-agent system message]` prefix on control messages the
  runtime injects into the agent's claude-code transcript (today
  used for the rate-limit retry kick; the prefix is documented in
  CLAUDE.md so the agent recognises future control messages
  without another prompt update).

### Changed

- Message processing is now thread-batched instead of per-message.
  Each root message id (`envelope_id` of the thread root) is a single
  slot in the priority queue; new messages on the same thread coalesce
  into the existing slot, a higher-priority arrival bumps the slot but
  preserves the batch cursor, and the consumer drains the full batch
  in one `on_message_batch` dispatch. The agent reads the whole thread
  and decides on its own who to reply to — no trigger-message concept.
- Cross-restart dedup: a new `thread_processing_state(root_id PK,
  last_processed_sent_at)` table records the last `sent_at` drained
  per thread. On startup the runtime skips anything at or below that
  cursor so a crash mid-batch doesn't replay the whole thread.
- Pre-dispatch jitter (0.0–1.5s) before each batch is sent to the
  agent. When multiple agents on the same host get activated by one
  broadcast message, unblocked dispatch sends them all into the
  claude-code API simultaneously and trips its rate limit; the
  random sleep desynchronises them. Each agent picks its own delay
  independently — no host-wide coordination needed.
- Status telemetry follows the thread-batch lifecycle. The first
  message in a batch still gets a single `/processing/start`
  (yellow dot lands there), but the per-message `/end` fan-out is
  replaced by a single `/messages/processing/end:batch` call that
  flips every message in the batch to green at once. Requires the
  puffo-server endpoint added in puffo-server#46 (deployed
  2026-05-11).
- `mcp__puffo__get_channel_history` now returns **root posts only**
  with a per-thread `(N replies)` annotation instead of inlining
  every reply. One busy thread no longer eats the whole channel's
  view. Same call also accepts `since=<envelope_id>` / `before=<ms>`
  / `after=<ms>` filters for incremental polling.
- Both `get_channel_history` and `get_thread_history` distinguish
  "unknown channel/thread" (HTTP 404 → "(no such channel: …)" /
  "(no such thread: …)") from "known but window-empty after
  filters" (HTTP 200 + `[]` → "(no … in the requested window)").
  Agents can tell a typoed id from a quiet polling window.

### Fixed

- Thread-batched dispatch could feed the agent the same envelope
  multiple times in one batch when the server's pending-message
  redelivery overlapped with live WS delivery (most easily reproduced
  after a daemon restart). `MessageStore.store` already used
  `INSERT OR IGNORE`, but the in-memory `_ThreadEntry.messages`
  batch had no envelope-level dedup. Now `_admit_thread_message`
  drops the append when the incoming `envelope_id` is already in the
  pending batch.
- A duplicate WS delivery that landed during a successful dispatch
  was being added to a fresh batch and re-fed to the agent on the
  next turn — observed as the same `envelope_id` appearing twice
  across consecutive claude-code turns (with a system-reminder
  injected between them). The handle_envelope cursor check couldn't
  catch it because `mark_thread_processed` runs *after* the dispatch
  finishes, and the in-batch dedup couldn't catch it because the
  consumer empties `entry.messages` to claim the batch. Adds a per-
  thread `dispatching_ids` set populated at claim time and consulted
  at the top of `_admit_thread_message`; cleared after the cursor
  advances on successful dispatch.
- Belt-and-suspenders: the consumer now dedupes `batch` by
  `envelope_id` one final time immediately before invoking
  `on_message_batch`, logging a warning if anything is dropped. The
  agent must never see the same envelope twice in one turn, even if
  some upstream race we haven't characterised slips through.
- `AgentAPIError` retry was duplicating the user input in the
  agent's claude-code transcript on every retry. When claude-code
  returns `API Error: Server is temporarily limiting requests`, the
  consumer re-enqueues the batch and `handle_message_batch` runs
  again — and the cli adapter `--resume`s the same claude-code
  session, so the same user message got appended to the transcript
  once per retry. A receiver who saw `×4` was hitting the rate
  limit four times before succeeding, with each attempt being a
  legitimate retry of one real wire delivery. New behaviour:
  preserve `--resume`, send a kick-only "session errored on rate
  limiting, please resume processing" instead of re-appending the
  batch. The cli adapter falls back to the full payload only when
  `_ResumeFailed` was caught and a fresh claude-code session was
  spawned (so the transcript has no original input to resume from).
  Plumbed via a new `Adapter.run_retry_turn(kick_text,
  fallback_user_message, ctx)` and a parallel
  `_consume_queue(on_api_error_retry=...)` callback. Capped at 3
  retries per batch; on exhaustion the cursor stays put so the
  failed envelopes remain readable via `get_channel_history` on
  the agent's next normal turn.
- cli-docker now auto-recreates a reused container when its
  `/opt/puffoagent-pkg` bind mount no longer resolves to the host
  path that contains `puffo_agent` (typical cause: the operator
  reinstalled puffo-agent from a different path, e.g. moved off
  `puffo-core-han-group/agent` to the standalone repo). Without
  this, `python3 -m puffo_agent.mcp.puffo_core_server` inside the
  container would fail with `ModuleNotFoundError`, claude-code's
  MCP subprocess wouldn't initialise, and every puffo MCP tool
  surfaced as "No such tool available". Detected by `docker exec
  test -f /opt/puffoagent-pkg/puffo_agent/__init__.py`.
- `AgentAPIError` retry path was clobbering mid-dispatch arrivals.
  When the dispatch failed AND a new message had landed on the same
  thread during the failed dispatch (admitted through the reopen
  branch of `_admit_thread_message`), the handler set
  `entry.messages = batch` and silently dropped the new message.
  The retry now dedupe-prepends the failed batch to whatever is
  already in `entry.messages` so the next attempt carries both.
- cli-local adapter silently dropped MCP servers passed via
  `--mcp-config`. claude-code gates MCP registration on the per-
  project trust dialog stored in `~/.claude.json`, and the dialog has
  no TUI surface under `--input-format stream-json`, so it never
  resolved. Switched the `bypassPermissions` permission-mode to emit
  `--dangerously-skip-permissions` (which also bypasses the trust
  dialog) instead of `--permission-mode bypassPermissions` (which
  only bypasses per-tool prompts). cli-docker already used the right
  flag; this aligns cli-local.

## [0.7.2] — 2026-05-10

First public PyPI release.

### Added

- Trusted Publishing workflows for PyPI and TestPyPI under
  `.github/workflows/`.

### Fixed

- `/spaces/<id>/events` pagination used the wrong query param
  (`cursor=` instead of `since=`); axum's `Query` extractor silently
  ignored it, so the agent's `_resolve_channel_name` loop fetched
  page 1 forever — pinning a worker's CPU and growing its WS receive
  queue until the host OOM'd. Also added a defensive strict-advance
  guard in the same loop and in the MCP `list_channels` tool, so a
  future server-side regression that echoes the same cursor back
  bails instead of spinning.

[Unreleased]: https://github.com/puffo-ai/puffo-agent/compare/v0.7.3...HEAD
[0.7.3]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.3
[0.7.2]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.2
