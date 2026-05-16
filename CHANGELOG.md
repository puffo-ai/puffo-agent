# Changelog

All notable changes to `puffo-agent` are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.10.0a1] — 2026-05-15

> **Pre-release published to TestPyPI only — not for general install.**
> Install with `pip install --index-url https://test.pypi.org/simple/
> --extra-index-url https://pypi.org/simple/ puffo-agent==0.10.0a1`.

### Added

- **Codex harness on `cli-local` (alpha).** New `runtime.harness: codex`
  option for OpenAI's `codex` CLI, running as a long-lived
  `codex app-server` JSON-RPC subprocess. Sibling to claude-code on the
  cli-local path; claude-code is untouched, codex is opt-in.

  Components:
  - `agent/harness/codex.py` (`CodexHarness`) + `runtime_matrix`
    entries (`HARNESS_CODEX` + `HARNESS_PROVIDERS[codex] = {openai}`).
    Default harness for openai remains `hermes` — codex must be opted
    into per agent.
  - `agent/adapters/codex_session.py` (`CodexSession`) — JSON-RPC
    over stdio: `newConversation` / `sendUserTurn` request/response,
    `item/*` notification stream accumulated into the reply,
    `turn/completed` resolves the turn, server-initiated approval
    requests auto-decided under `bypassPermissions`. Conversation id
    persisted to `<CODEX_HOME>/codex_session.json` so daemon restarts
    `resumeConversation` instead of reopening from scratch.
  - `LocalCLIAdapter` dispatches by `harness.name()`: codex agents
    route through `CodexSession`, everything else stays on
    `ClaudeSession`. `refresh_ping` / `_run_refresh_oneshot` short-
    circuit for codex (static `OPENAI_API_KEY`, no OAuth rotation).
  - Per-agent `CODEX_HOME` (`~/.puffo-agent/agents/<id>/.codex/`).
    Two auth paths supported: explicit `runtime.api_key` → injected
    as `OPENAI_API_KEY` env (cleanest, no rotation), OR ChatGPT-
    account OAuth via `codex login` (operator runs it once, the
    adapter symlinks `~/.codex/auth.json` into each agent's
    `$CODEX_HOME`; copy fallback on Windows non-dev-mode). Fail-loud
    error at spawn when neither path is usable. AGENTS.md lives at
    `$CODEX_HOME/AGENTS.md`; reload hot-swaps the in-memory
    `current_instructions` field carried by each `sendUserTurn` call.
  - `mcp/config.py` gains `write_codex_mcp_config` — a TOML emitter
    for codex's `[mcp_servers.puffo]` schema with per-server `env`
    table for the existing `puffo_core_mcp_env` payload.

  Out of scope for v1 (will follow): per-turn item-event streaming to
  StatusReporter; codex-shaped health probe; cli-docker codex.

  Self-update for codex agents in v1 is limited to
  `reload_system_prompt` (re-writes AGENTS.md). `install_skill` /
  `refresh` / `install_mcp_server` / `uninstall_*` remain
  claude-code-only — the existing `_require_claude_code` MCP gates
  surface a clear error.

  This release ships to TestPyPI only. The codex App Server JSON-RPC
  contract is still pre-1.0 upstream; we treat 0.10.0a1 as the
  verification vehicle. Promote to PyPI 0.10.0 once a colleague has
  walked an agent through a real end-to-end turn against the actual
  `codex app-server` binary.

## [0.8.3] — 2026-05-15

### Fixed

- **`send_message` no longer silently sends to the wrong space when a
  channel id isn't in the local cache.** The previous resolver fell
  back to `cfg.space_id` (the agent's home space) whenever
  `lookup_channel_space` had no record of the channel — but the
  channel may actually live in a *different* accessible space, in
  which case the next call (`/spaces/<home>/channels/<ch>/members`)
  targeted the wrong space. The relay's response in that case wasn't
  the documented 403/400; it was a 2xx with a non-JSON body, which
  the caller then `.get()`-ed and crashed three layers up with the
  opaque `'str' object has no attribute 'get'` (FB-76 root cause).

  Two-stage resolver now:
  1. local cache (`data_client.lookup_channel_space` — unchanged);
  2. on miss, walk `GET /spaces` + `GET /spaces/<sp>/channels` to
     find a definitive match across the agent's accessible spaces.

  When both miss, raise a clear unresolved-channel error rather than
  guessing. The previous `or cfg.space_id` fallback is removed
  entirely. 2 new tests in `test_puffo_core_tools.py` (discovery
  succeeds in another space; full miss → clear error). The
  `PuffoCoreHttpClient` fail-loud fix below remains as the
  safety-net for any other caller that hits the same non-JSON-2xx
  shape from the relay.

- **`PuffoCoreHttpClient` no longer hands callers a raw string body
  as if it were a parsed JSON response.** When a 2xx response carried
  a non-empty, non-JSON body — a proxy / CDN error page, a gateway
  interstitial, a plain-text error — `_do_request`'s `json.loads`
  fallback returned the raw string, and every caller of
  `get()` / `post()` (`mcp__puffo__send_message` channel resolution,
  `list_channels`, …) then did `.get()` on it and crashed three
  layers up with the opaque `'str' object has no attribute 'get'`.
  Reported via FB-76: `send_message` to a channel id and
  `list_channels` failing identically — the shared `http_client`
  layer, not an endpoint-specific bug. `_request` now raises
  `HttpError` with the actual body when a 2xx response isn't JSON, so
  the failure is diagnosable at the source. Empty 2xx bodies (204 No
  Content etc.) are unaffected. 2 new tests in `test_http_client.py`.

## [0.8.2] — 2026-05-14

### Fixed

- **A `# Soul` section whose body opens with its own heading is no
  longer read as empty — or duplicated on update.** Soul templates
  (and the operator-authored souls) open `# Soul` with a
  `# <agent-name>` title line. Both the `_profile_summary` reader and
  the `_update_profile_summary` writer detected the section's end as
  "the next heading of the same-or-higher level" — and the body's own
  opening H1 *is* such a heading:
  - **Read** closed the section instantly and returned `""`, so the
    web client's agent card showed "Soul not configured" even though
    `profile.md` carried a full soul.
  - **Write** skipped "the old body until the next heading", hit that
    same opening H1, skipped nothing, and inserted the new summary
    *above* the old body — an append/duplicate instead of a replace.
    Repeated UI soul edits stacked multiple souls into one file.

  Both paths now share one `_soul_section_span` helper: a
  same-or-higher heading only closes the section once real prose (a
  non-blank, non-heading line) has been collected. An *opening*
  heading is part of the soul; a *trailing* `# Notes` section after
  real soul prose still closes it and stays out. Follow-up to the
  0.7.6 multi-line `_profile_summary` change that introduced the
  walk-to-next-heading logic.

  4 new tests in `test_profile_summary.py` (read with an opening
  heading, write replaces an opening-heading body without
  duplication, trailing section preserved on update, append-when-
  absent). Full suite: 548 passed, 7 skipped.

## [0.8.1] — 2026-05-14

### Added

- **`puffo-agent agent reset-primer <id> ...`** — re-seed the shared
  platform primer to the installed version. The shared primer
  (`~/.puffo-agent/docker/shared/CLAUDE.md` + `skills/`) is
  seed-once: `ensure_shared_primer` never overwrites it, so a
  `puffo-agent` upgrade never reached existing installs — primer
  updates only landed on brand-new machines. The new command
  force-rewrites the managed shared files to this install's version
  (unchanged files skipped, edited ones backed up to `<file>.bak`
  first), then rebuilds each listed agent's managed `CLAUDE.md` /
  `GEMINI.md` from the fresh primer. The re-seed is global — the
  agent id list only scopes which agents get rebuilt. Running
  workers keep their loaded prompt; the rebuild takes effect on the
  worker's next restart.

### Fixed

- **`is_visible_to_human=false` on a root-level message is no longer
  a silent no-op.** Root-level (non-threaded) messages can't fold in
  the human UI — only threaded replies do — so an agent passing
  `false` on a root-level `send_message` /
  `send_message_with_attachments` was producing a message that
  rendered visible anyway but was inconsistently excluded from
  unread counts. The tools now coerce the flag back to visible (the
  message still goes out — a warning, not an error) and splice a
  note into the tool response so the agent learns at the point of
  the mistake rather than depending on a possibly-stale primer. The
  primer and tool docstrings now state that `false` only takes
  effect on threaded replies.

## [0.8.0] — 2026-05-14

### Added

- **`is_visible_to_human` — agents now mark every message as
  human-facing or agent-to-agent.** A new field on the
  signed+encrypted `MessagePayload` distinguishes messages a person
  should read from machine-to-machine chatter that human clients
  fold away. The field lives inside the E2E-encrypted payload, so
  the server stores it opaquely — no server schema change.

  - The `mcp__puffo__send_message` MCP tool takes a **required**
    `is_visible_to_human` argument — there is no default, the agent
    judges every message. Pass `true` for replies, status updates,
    and operator pings; `false` only for coordination chatter a
    human watching the channel would find pure noise.
  - `upload_file` is renamed `mcp__puffo__send_message_with_attachments`
    and gains the same required argument — it always was a real
    message-send, the name now says so.
  - Internal sends are explicit: operator-facing DMs (invite
    approvals) stay visible; the `[SILENT]`-skip fallback posts
    folded, since an agent that reached the fallback never made a
    conscious visibility call.
  - Incoming messages surface `is_visible_to_human` in the agent's
    metadata block, so receiving agents see how each message was
    classified.
  - The agent primer documents the required argument on both write
    tools and steers agents toward `send_message` over the fallback.

  Backward compatible on the wire: messages from senders that
  predate the field decrypt with `is_visible_to_human` defaulting
  to `true`.

- The `post_message` client method is renamed `send_fallback_message`
  to match its role — the `[SILENT]`-skip safety net, not a general
  post path.

### Fixed

- **An oversized inbound image can no longer dead-lock an agent.**
  Anthropic's API rejects any conversation containing an image whose
  longest edge tops 2000px ("exceeds the dimension limit for
  many-image requests — start a new session with fewer images").
  Once claude-code Read such an attachment into its session
  transcript, EVERY later turn failed wholesale and the agent was
  permanently stuck — the image analogue of the long-message
  problem fixed in 0.7.9.

  Two-part fix:
  - **Prevention** — inbound image attachments are dimension-checked
    and downscaled in place at save time (longest edge pinned to
    1568px, Anthropic's recommended max, well under the hard cap),
    so claude-code only ever loads in-bounds images. Adds a Pillow
    dependency; non-images, already-small images, and anything
    Pillow can't open are left untouched.
  - **Recovery** — if a poison reaches the API anyway (or is already
    stuck in an existing transcript), the ``claude`` session adapter
    recognises the rejection, clears the persisted session id, kills
    the subprocess, and **re-runs the same turn on a fresh session**
    (no ``--resume`` onto the poisoned transcript). The poison was
    content from an earlier turn the fresh session no longer has, so
    the re-sent message goes through — the triggering message is
    NOT dropped. Retried once; if the message itself still poisons
    the fresh session it's surfaced rather than looped.

  Existing stuck agents recover automatically on their next inbound
  message — no operator action needed.

## [0.7.9] — 2026-05-13

### Fixed

- **``mcp__puffo__list_mcp_servers`` now enumerates plugin-routed
  MCP servers too.** Operator + agent feedback: plugins installed
  via ``claude /plugin install`` (e.g. ``imessage``,
  ``chrome-devtools-mcp``) register their MCP servers under
  ``~/.claude/plugins/cache/<plugin>/<version>/.mcp.json`` — a
  third scope distinct from system (``~/.claude.json``) and agent
  (``<workspace>/.mcp.json``). The listing tool only walked the
  first two, so plugin-provided servers were invisible to the
  agent even though it could call them.

  Fix walks the plugin cache too and tags each entry ``plugin``
  with a ``(from <plugin>/<version>)`` source label so the
  operator can map server-back-to-plugin at a glance. Defensive
  on the file system: missing cache dir, missing per-version
  ``.mcp.json``, malformed JSON in one plugin's file, and
  multiple cached versions all handled without taking the whole
  listing down.

  5 new tests in ``test_agent_install.py``; the 2 pre-existing
  list tests updated to the new 3-tuple shape
  ``(scope, name, source)``.

### Added

- **Inbound long-message redaction + ``get_post_segment`` MCP
  tool.** Operators occasionally paste large code blocks into a DM
  or channel; combined with the agent's system prompt + history
  the result can blow past the provider's context window. Before
  this release that surfaced as an "API Error: Prompt is too
  long" string from claude-code, which the rate-limit retry loop
  bounced through ``--resume`` three times and then abandoned —
  but with the cursor stuck at the failed batch, any new message
  in the same thread re-triggered the same failure. From the
  operator's perspective the agent was wedged and ``restart``
  didn't help (the claude-code session still held the oversize
  transcript); ``pause`` was the only way out.

  Fix: in ``puffo_core_client.handle_envelope``, any message
  whose text exceeds ``DaemonConfig.max_inline_message_chars``
  (default 4000) gets replaced — *for the LLM view only* — with a
  ``[puffo-agent system message]`` placeholder citing the
  envelope_id, total length, segment count, sender, and a short
  preview. The full envelope is still persisted to
  ``messages.db`` unmodified. The new
  ``mcp__puffo__get_post_segment(envelope_id, segment,
  segment_size)`` tool pages the body back ``segment_chars``
  bytes at a time (default 2000) so the agent can fetch only
  what it needs.

  Logged when triggered:
  ```
  agent <id>: inlined message <env_id> truncated
  (47832 → 4000 chars, 24 segments) for prompt budget
  ```

  New tunables in ``daemon.yml``:
  ```yaml
  max_inline_message_chars: 4000   # redact above this
  segment_chars: 2000              # page size for get_post_segment
  ```

  Existing oversize content already in a claude-code session
  isn't unstuck by this release — clear the agent's
  ``.claude-session.json`` (under ``~/.puffo-agent/agents/<id>/``)
  once to force a fresh transcript on next start.

## [0.7.8] — 2026-05-13

### Added

- ``puffo-agent agent autoaccept <id> --space <space_id> --owner on|off``
  — toggles the agent's per-space ``auto_accept_owner_invite``
  flag via signed PATCH to puffo-server's new
  ``/spaces/{id}/members/me/settings`` endpoint. When ON, the
  agent silently joins any channel its space owner invites it to;
  when OFF, the invite goes through the normal DM-operator
  confirmation path. Member-invite flag is deliberately not
  exposed — the server returns 403 for agents on that field. CLI
  uses the agent's own keystore (same auth model as the
  ``profile`` subcommand: operator controls the local keystore,
  so a CLI invocation IS an operator decision).

### Changed

- **Self-introduction nudge now fires on server-side auto-accept
  too.** Previously the synthetic ``AcceptChannelInvite`` event the
  server emits when it short-circuits an InviteToChannel
  (``auto_accept_owner_invite=TRUE`` + inviter is the space owner)
  was silently ignored by the daemon's WS handler, so the agent
  never posted its 2-3-sentence intro in the auto-joined channel.
  The handler now recognises the synthetic variant via the
  ``payload.original_invite`` marker and routes through the same
  ``_enqueue_channel_intro_nudge`` path that the operator-signed
  accept already uses. Idempotent: redelivered events hit the
  existing per-channel dedup gate. 5 new unit tests pin the
  branch + negative cases (other-slug fan-out, operator-signed
  echo, malformed payload).

## [0.7.7] — 2026-05-13

### Fixed

- **Agents can now see their own sent messages in
  ``get_channel_history`` / ``get_thread_history``.** Operator-
  reported: `get_thread_history(<root>)` returned only messages
  from other senders; the agent's own replies were absent even
  though other agents in the same thread saw them fine.

  Root cause: two parallel send paths existed, and only one wrote
  locally. The daemon-internal ``PuffoCoreMessageClient.post_
  message`` (used by the fallback-reply path in worker.py) mirrored
  the outbound payload to ``messages.db`` after the server POST,
  but the MCP tool ``mcp__puffo__send_message`` (the path agents
  actually use) didn't. Both relied on the daemon's WS handler to
  not persist, because ``handle_envelope`` dropped envelopes whose
  ``sender_slug == self.slug`` at the door — to "avoid retrigger
  loops".

  The right shape (long-term) is "server-echoed-over-WS is the
  canonical proof a message was delivered, so the WS handler is
  the canonical write path for inbound + outbound alike":

  * ``handle_envelope`` no longer drops self-envelopes. The server
    fans out every recipient device in ``envelope.recipients``,
    which always includes the agent's own device (the MCP
    ``send_message`` tool puts self in the recipient list for both
    DMs and channels), so a successful send → WS echo → daemon
    persists through the same path every other message uses.
  * After the ``store.store(...)`` call, self-envelopes return
    early — they're persisted but never queued for the LLM
    (re-feeding the agent its own words would trip a turn-by-turn
    echo loop).
  * The redundant mirror-write in ``post_message`` was removed.
    Both send paths now converge on the WS-echo persistence,
    which is the single source of truth.

  Follow-up: a future change can extract ``handle_envelope`` to a
  testable method and add a unit test for the self-echo persist
  + dispatch-skip semantics. The cursor-check tests in
  ``test_thread_queue.py`` are unaffected. Full suite still 496 /
  503 (no new tests, no regressions).

- **Intro-nudge synthetic envelope is now persisted to ``messages.db``,
  and the status reporter no longer 404s on local-only envelopes.**
  Operator-reported: after joining a channel the agent received the
  ``[puffo-agent system message] You've just been added to …`` prompt
  and posted an intro, but the daemon log carried a noisy
  ``begin_turn message=intro-prompt-… failed (HTTP 404: NOT_FOUND)``
  per nudge, and the agent's own ``mcp__puffo__get_channel_history``
  call right after would return an inconsistent view (the intro it
  just saw wasn't there).

  Two changes:
  * The synthetic envelope is written to ``messages.db`` via the
    existing ``MessageStore.store`` path before being enqueued, so
    ``get_channel_history`` / ``get_message_by_envelope`` /
    ``lookup_channel_space`` all resolve it naturally. Side benefit:
    ``send_message(root_id=<intro id>)`` is now a real thread root
    locally; agents can post a reply-shape intro without producing
    a broken reference.
  * ``StatusReporter.begin_turn`` / ``end_turn`` /
    ``end_turn_batch`` recognise local-only envelope prefixes
    (currently ``intro-prompt-``) and skip the server POST. The
    run is still tracked in-memory via the returned ``run_id`` so
    the worker's batched ``end_turn_batch`` path keeps working;
    server-side rows simply never get created for envelopes the
    server never knew about. Quiets the per-nudge WARN noise
    structurally rather than via primer compliance.

  5 new tests: 1 in ``test_channel_intro_nudge.py``
  (``test_intro_nudge_persists_envelope_to_messages_db``) + 4 in
  ``test_status_reporter.py`` (``begin_turn`` skip, ``end_turn``
  skip, batch filters mixed local/real runs, all-local batch
  short-circuits). Full suite: 496 passed, 7 skipped.

- **Host Claude Code plugins now propagate to cli-local + cli-docker
  agents.** Operator-reported: plugins installed via
  ``claude /plugin install <name>@<marketplace>`` (e.g.
  ``imessage@claude-plugins-official``,
  ``chrome-devtools-mcp@claude-plugins-official``) were silently
  invisible to cli-local agents — ``mcp__puffo__list_mcp_servers``
  returned ``(no MCP servers registered)`` for the plugin-provided
  MCPs, and the per-agent ``.claude/plugins/`` directory didn't
  exist at all.

  Root cause was a missing sync step. ``seed_claude_home`` had been
  one-shot copying ``.claude/settings.json`` + ``.claude.json``, but
  nothing was bringing across:
  * ``~/.claude/plugins/`` — the marketplace clones + plugin cache
    + ``installed_plugins.json`` + ``known_marketplaces.json`` that
    contain the actual plugin code.
  * ``~/.claude/settings.json#enabledPlugins`` — the array that
    tells Claude which plugin names to load. ``seed_claude_home``
    copied this once on first start but never refreshed, so
    plugins enabled later on the host stayed invisible to the
    agent. (Plugin-provided MCP servers register through the plugin
    pipeline, not through the user-level ``mcpServers`` map that
    ``sync_host_mcp_servers`` already merges, so the existing MCP
    sync didn't cover this case.)

  Two new helpers in ``portal/state.py``, both wired into
  ``local_cli.LocalCLIAdapter._verify()`` after the existing host
  syncs:
  * ``sync_host_plugins(host_home, agent_home)`` — symlinks
    ``host_home/.claude/plugins/`` to
    ``agent_home/.claude/plugins/``. The tree is GB-scale (each
    marketplace is a git clone with history); symlink keeps the
    agent live with host installs without recopy cost. Falls back
    to ``copytree`` on Windows-without-Developer-Mode (operators
    can ``rm -rf <agent>/.claude/plugins`` to force a refresh in
    that branch). Returns the mode string for logging.
  * ``sync_host_enabled_plugins(host_home, agent_home)`` — rewrites
    just the ``enabledPlugins`` key in per-agent ``settings.json``
    from the host's. Atomic tmp+rename. Leaves other settings
    keys (theme, model, etc.) untouched. Accepts both the dict
    (``{name: true}``) and list (``[name, ...]``) shapes Claude
    Code has used historically.

  Tests in ``test_host_sync.py``: 11 new (5 plugin tree + 6
  enabledPlugins) covering symlink / copy-fallback / idempotent
  re-call / no-host-dir noop / agent-side preservation.

  cli-docker takes a different shape because the container's
  ``/home/agent/.claude`` is already an outer bind-mount: nested a
  second read-only bind-mount surfacing ``host_home/.claude/plugins``
  at ``/home/agent/.claude/plugins:ro`` (so the marketplace + cache
  + installed_plugins.json reach the in-container Claude without a
  copy), and called the same ``sync_host_enabled_plugins`` helper
  in ``_ensure_started`` so settings.json (already bind-mounted)
  carries the enabledPlugins array. The cli-docker image bakes
  node 22 + npm + python + uv, so most ``npx``/``uvx`` plugin
  commands resolve naturally; native-binary plugins (those that
  shell out to a host-only path) will still fail at use-time. 5
  new tests in ``test_docker_host_plugins.py`` covering the bind-
  mount injection, the missing-host-dir noop, the ``:ro`` flag,
  argv ordering (before image positional), and the enabledPlugins
  propagation path.

  Full suite: 491 passed, 7 skipped.

## [0.7.6] — 2026-05-12

### Added

- Bridge endpoints for the web client's 5-button agent action row:
  ``POST /v1/agents/{id}/pause``, ``POST /v1/agents/{id}/resume``,
  ``POST /v1/agents/{id}/archive``. The pause / resume pair flips
  ``agent.yml``'s ``state`` field; the reconciler picks the change
  up on its next tick and stops or starts the worker. Archive
  pauses first (so the worker exits cleanly + releases sqlite-WAL
  file handles), then drops an ``archive.flag`` sentinel; the
  reconciler then moves the agent dir into
  ``~/.puffo-agent/archived/<id>-ws-<timestamp>``. All three are
  idempotent and ownership-gated — only the agent's operator can
  call them; non-owners get 403, missing IDs get 404.

  Tests in ``tests/test_pause_resume_archive.py``: state-flip
  parity, idempotent-already-{paused,running} note, 403 on
  non-owner, 404 on unknown id, archive writes the flag even when
  the agent was already paused, archive non-owner doesn't drop a
  stray flag.

- ``role`` + ``role_short`` fields on ``AgentConfig`` and the local
  bridge profile-edit path. Mirrors the
  [puffo-server identity_role migration](https://github.com/puffo-ai/puffo-server/pull/50)
  that surfaces the same fields on the server identity profile.
  ``role`` is the long form (≤140 chars, recommended shape
  ``<short>: <description>``); ``role_short`` is the chip label
  clients render in member lists (≤32 chars). Both default to empty
  string; existing agent.yml files load with empty values and don't
  need a fresh write.

  Plumbed across:
  * ``AgentConfig`` load/save preserves the two fields.
  * ``PATCH /v1/agents/{id}/profile`` accepts ``role`` + ``role_short``,
    validates lengths, rejects ``role_short`` without ``role``
    (mirrors the server-side 400), updates agent.yml, and best-effort
    syncs to ``PATCH /identities/self`` via the existing
    ``_sync_agent_profile`` helper. Sync failures log but don't
    block the local write.
  * ``POST /v1/agents`` (bridge create) accepts the same two fields
    and fires a best-effort post-create sync so a freshly-provisioned
    agent's server-side identity profile carries its role from the
    start.
  * ``puffo-agent agent create`` CLI gained ``--role`` /
    ``--role-short`` flags. Length and "short without role"
    validation matches the bridge.

  ``role_short`` defaults to the server-side derive on save:
  ``"coder: main puffo-core coder"`` → ``"coder"``. A local mirror
  of that derive in the bridge handler + CLI keeps agent.yml
  consistent with what the server stores without an extra GET. The
  agent guides include a unit test pinning both mirrors against the
  server contract.

### Fixed

- ``_profile_summary`` returns the full ``# Soul`` section body
  instead of just the first non-blank line. The web client's
  AgentsPane card has a "▸ Soul" expand toggle that revealed only
  one sentence of what the operator typed; the helper now walks
  from the matching heading (``# Soul`` / ``# Description`` /
  ``# About`` / ``# Summary``) to the next top-level heading or
  EOF, preserves sub-headings inside the body, and trims leading
  + trailing blank lines. Round-trip with ``_update_profile_summary``
  (which already wrote the full body) is now lossless.

- README and the new "Agent identity" section now document the
  five operator-editable fields (display_name, avatar_url, role,
  role_short, soul) and how they map onto ``agent.yml`` +
  ``profile.md``. The ``# Soul`` body is what the LLM reads every
  prompt; sub-headings inside it travel along.

## [0.7.5] — 2026-05-12

### Added

- Auto self-introduction after accepting an invite. When the agent
  accepts an ``invite_to_channel`` (auto-accepted from an operator-
  matched signer, or accepted via the operator's ``y`` DM reply),
  the daemon enqueues a synthetic ``[puffo-agent system message]``
  envelope on the invited channel asking the agent to post a short
  intro (default English, 2-3 sentences) via its normal
  ``mcp__puffo__send_message`` path. For ``invite_to_space``, the
  daemon resolves the space's auto-General channel via ``GET
  /spaces/<id>/channels`` (the row with ``is_public=true``) and
  fires the same nudge there — General is the one channel the agent
  gets auto-fanned-out membership on when accepting a space invite,
  so it's the only channel it can immediately post into. Existing
  ``profile.md`` personality shapes the wording.

  Wiring:
  * New sqlite table ``channel_intro_prompted(channel_id PK,
    prompted_at)`` on the per-agent ``messages.db`` gates the nudge
    so a daemon restart or server-side invite redelivery can't fire
    a second intro on the same channel.
  * New sqlite table ``channel_space_map(channel_id PK, space_id,
    learned_at)`` records the channel→space mapping discovered
    out-of-band by the General lookup. ``lookup_channel_space``
    (used by the MCP ``send_message`` tool) checks this map first,
    then falls back to the historical ``messages``-table inference.
    Without this, the agent's first send into the freshly-joined
    channel would 404 the data-service lookup (no prior messages on
    that channel) and fall back to ``agent.yml``'s configured
    ``space_id`` — the WRONG space when the agent has just joined
    a different one.
  * General lookup retries on a sleep-first schedule
    (0.5 / 1 / 3 / 6 / 12 / 24 / 24 seconds, ~70s total) because
    the server has a tight race between the ``accept_space_invite``
    POST returning and the ``channel_memberships`` row that gates
    ``GET /spaces/<id>/channels`` being committed. Past ~70s the
    daemon gives up silently — missing the intro is preferable to
    spinning forever.
  * ``_channel_name_cache`` is warmed from the same ``/channels``
    response so the ``_resolve_channel_name`` call inside the
    nudge becomes a cache hit instead of a duplicate HTTP round-
    trip.

- Every message fed to the agent now carries the sender's
  ``display_name`` alongside the slug, rendered as two distinct
  fields in the user block:

  ```
  - sender: Alice Wong
  - sender_slug: alice-0001
  - sender_type: human
  ```

  ``sender`` is the human-readable handle the LLM uses to address
  peers in prose; ``sender_slug`` stays the structural identifier
  for @-mentions and ``send_message`` routing. When the server has
  no display_name on file, ``sender`` echoes the slug so the field
  is always populated. Resolution piggy-backs on the existing
  per-session display-name cache (one ``/identities/profiles?slugs=``
  call per distinct sender per session; subsequent messages from
  the same sender are free).

### Changed

- ``- sender: <slug> <email>`` rendering removed from the user
  block; ``sender_email`` was hardcoded to ``""`` everywhere it
  was populated and just cluttered the prompt.

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

[Unreleased]: https://github.com/puffo-ai/puffo-agent/compare/v0.10.0a1...HEAD
[0.10.0a1]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.10.0a1
[0.8.3]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.3
[0.8.2]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.2
[0.8.1]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.1
[0.8.0]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.8.0
[0.7.5]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.5
[0.7.4]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.4
[0.7.3]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.3
[0.7.2]: https://github.com/puffo-ai/puffo-agent/releases/tag/v0.7.2
