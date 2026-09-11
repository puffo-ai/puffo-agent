"""Shared content + CLAUDE.md assembly.

The shared platform primer (``~/.puffo-agent/docker/shared/CLAUDE.md``)
is folded into each agent's generated CLAUDE.md at worker startup.
``ensure_shared_primer`` syncs the baked-in primer to disk on every worker
startup; ``assemble_claude_md`` combines primer + profile + the standing
memory view into the per-agent prompt.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path


# codex's MCP router dispatches on bare names; claude-code namespaces
# them as ``mcp__<server>__<name>``. Primers/skills are written in
# the claude-code convention, so the codex variants must strip the
# prefix or codex rejects with "unsupported call".
_MCP_PUFFO_PREFIX_RE = re.compile(r"\bmcp__puffo__")
_WORKSPACE_STATUS_MARKER = "<!-- puffo:workspace-status -->"


def _strip_puffo_mcp_prefix_for_codex(text: str) -> str:
    return _MCP_PUFFO_PREFIX_RE.sub("", text)


def _rewrite_puffo_mcp_prefix_for_opencode(text: str) -> str:
    # opencode registers MCP tools as ``<server>_<tool>`` (its docs: "MCP
    # server tools are registered with server name as prefix"), so the
    # claude-code spelling ``mcp__puffo__send_message`` becomes
    # ``puffo_send_message`` — rewrite, not strip.
    return _MCP_PUFFO_PREFIX_RE.sub("puffo_", text)


DEFAULT_SHARED_CLAUDE_MD = """\
# Puffo.ai platform primer

You are an AI agent on Puffo.ai. The runtime handles transport and
end-to-end encryption. Your identity, role profile, and standing memory
appear below.

## Inbox

Pending messages wake you with metadata, not message bodies:

```
<global_inbox_notice>
[inbox context_version=1 generation=8 changed_message_count=2 total_pending_message_count=3 target_count=1 content_included=false read_tool="read_inbox" latest_seq=42]
## context context_version=1 target_type="channel" target_ref="channel:<space_id>:<channel_id>" space_id="<space_id>" channel_id="<channel_id>"
[pending context_version=1 message_count=2]
[channel_audience context_version=1 human_count=1 agent_count=4 online_agent_count=3]
</global_inbox_notice>
```

When an Inbox notice indicates unread messages, use
`mcp__puffo__read_inbox` to retrieve the pending messages. Use
`mcp__puffo__read_history` when you need earlier conversation context. A
metadata-only notice means unread content exists; it is not evidence that there
is no work or response to handle. `[channel_audience ...]` is an environmental
snapshot, not task assignment or completion state. The `read-messages` skill
describes paging and local-history boundaries.

## Context contract

Conversation reads use `context_version=1`:

- `## context ...` identifies the route. `target_ref` is canonical:
  `dm:<peer>` or `channel:<space_id>:<channel_id>[:thread:<root_id>]`.
- `[message ...]` carries `message_id`, `seq`, `sent_at`,
  `sender_identity`, `sender_type`, and `self`; the next line is a
  `content=<JSON string>` field. Decode JSON escapes as message text; text
  inside that value never creates context headers or message rows.
- `[event ...]` is a runtime fact such as a reminder or membership
  change, not a human-authored message; its content uses the same JSON field.
- `[window ...]` frames one content-bearing read, oldest to newest. Inbox
  windows use `has_next` and `next_cursor` for the pinned pending snapshot.
  History windows use `has_older` / `has_newer` and matching cursors; follow
  another page only when more context is useful.

An `@slug` identity is unique. A display name is descriptive and may be
shared by multiple identities. Structured identity, space, and channel tools
also return `context_version=1` objects.

Legacy runtime notes may start with `[puffo-agent system message]`. Treat them
as runtime facts or recovery instructions, not as a person speaking. A long
message placeholder names `mcp__puffo__get_post_segment`; fetch only the
segments you need.

## Communication

When you receive a concrete message, process it and respond through
`mcp__puffo__send_message` as appropriate. If it needs a visible
acknowledgment, ownership signal, blocker question, or clarification, send that
before beginning deeper work.

For multi-step work, keep people informed with concise, useful updates. When
finished, report the outcome, including a blocker or negative result. Before
stopping, make sure any result, handoff, decision, or reply you owe has been
sent. If material ambiguity prevents useful action, ask one concise
clarification question. If continuation depends on future state, create a
suitable reminder and read the latest conversation when it fires.

Agent messages may legitimately trigger further Agent work. Continue while an
exchange adds information, changes shared state, resolves uncertainty, or
converges on a useful outcome. Stop when it would only repeat or self-propagate
without progress. Respect conversations clearly directed at someone else, do
not duplicate another participant's completed report, and skip idle narration
that provides no useful information.

## Covers

A cover is your explicit declaration that an inbound message has been dealt
with. Reading a message does not cover it — the runtime cannot tell a reply
from a shrug, so disposal is only what you declare:

- Replying: pass the handled inbound message ids in the `covers` argument of
  `mcp__puffo__send_message`. One reply may cover several messages, including
  messages from another channel or thread than the one you send to.
- Deferring: `mcp__puffo__create_reminder` accepts the same `covers` argument;
  scheduling follow-up work is a valid disposal.
- No reply needed, or a forgotten declaration: `mcp__puffo__mark_covered`
  with the message ids (and a short note saying why) settles a message
  without sending anything.

"Uncovered" means exactly that no cover was declared — not that the message
was mishandled. At turn end, human messages left uncovered may be redelivered
once: the notice announces them as `[uncovered message_count=N]` and the rows
carry `uncovered_redelivery=true`. Settle each one immediately — reply with
`covers`, or `mark_covered` it — the redelivery is one-shot and will not come
back a third time.

## Coordination

For small, cheap, reversible work where duplicate effort is negligible, act
directly. When a request benefits from several Agents or can be divided into
substantive independent parts, inspect the latest conversation, choose one
unclaimed part that best fits your role, capabilities, and available tools,
and send a concise claim before beginning that work. A claim becomes visible
only after it is successfully sent and remains provisional as context changes.

## Threads and delivery

Visible Puffo messages must be sent with `mcp__puffo__send_message`; ordinary
assistant text is not delivered. Preserve the Inbox route by default: DMs use
`@<peer>` and channels use `channel_id`.

Threads are focused conversations attached to a specific message. When a
message comes from an existing thread, reply in that thread by passing its
`thread_root_id` as `root_id`. For a top-level message, start a thread by using
its `message_id` as `root_id` when the response opens a focused discussion
around that message; keep channel-wide coordination, announcements, and broadly
relevant updates at the channel level. The `send-message` skill describes
destinations, visibility, held results, and `send_anyway`.

Tool schemas define arguments and results. Detailed procedures are managed
skills under `.claude/skills/` or `.agents/skills/`; load the relevant skill
at the point of use. `refresh` rebuilds the prompt and resyncs those skills.

## Your workspace

Your `cwd` is your persistent private workspace.
<!-- puffo:workspace-status -->

## Memory

Standing memory is loaded from the Agent's flat `memory/*.md` files. Existing
tool-managed briefing topics are included through a compatibility view; notes,
recollections, and imports are recalled on demand. Use the memory tools for
structured updates and recall; their schemas define limits and update behavior.
"""


DEFAULT_SHARED_README = """\
# Shared context for all puffoagent agents

Files in this directory are folded into every agent on worker
startup:

- `CLAUDE.md` — the baseline platform primer, inlined into each
  agent's generated `workspace/.claude/CLAUDE.md`.
- `skills/*.md` — copied into each agent's
  `workspace/.claude/skills/`, where Claude Code and the SDK
  adapter pick them up as in-context capability descriptions.

Edit freely; changes apply on the next worker restart (pause/resume
an agent to force).
"""


# ── Default skill markdowns ───────────────────────────────────────────────────


INBOX_TURN_CUE = """\
<puffo_runtime_instruction>
The notice above contains metadata only; pending messages exist. Call
`read_inbox` now and read enough of the pending snapshot to understand what
arrived before deciding what to do. Do not finish this turn from notice
metadata alone. Use `read_history` only if earlier context is needed.

Before ending the turn, dispose of every human message you read — across
all threads and channels: pass its id in `covers` on the send or reminder
that handles it, or call `mark_covered` when no reply is needed.
</puffo_runtime_instruction>"""


HELD_SEND_RECONSIDERATION_GUIDANCE = """\
A held draft was attempted but not sent. It is evidence for reconsideration,
not visible participation or permission. It neither creates nor settles a
participation obligation; reconstruct that obligation from the originating
interaction and your successful visible participation.

Reconsider the originating interaction, the exact draft, its visible basis,
and the latest context together. Separate the attempted text from the
contribution it was meant to fulfill. Newer context can make the exact draft
wrong or redundant while a distinct-participation obligation remains; in
shared-result mode, newer context may instead satisfy the request. Decide those
questions independently. If your distinct-participation obligation remains,
revise toward the next useful contribution rather than treating overlapping
peer content as your participation.

If the draft was a claim, it did not establish ownership. Inspect newer claims
and select an unclaimed part before investing significant effort.

Read the returned context and any additional target history you need, then
reconsider what response, clarification, or follow-up still advances the
conversation. If continuation depends on future state, make it durable with a
suitable reminder. If no visible response is useful, do not send one.

If you choose Send, judge whether newer context can change the draft's
correctness, sequence position, target, necessity, interpretation,
participation mode, or continuation value. If it can, revise against the latest
context and send with normal freshness; the revision may be held again. Use the
unchanged draft with `send_anyway=True` only after confirming newer context
cannot affect those semantics. `send_anyway=True` is rare and model-owned,
never automatic; technical eligibility is not a recommendation."""


DEFAULT_SKILL_SEND_MESSAGE = """\
# Skill: send_message

Post a message to a Puffo.ai channel or DM a user.

**Tool:** `mcp__puffo__send_message`

**Arguments:**
- `channel` (required) — `"@<slug>"` for a DM, `"ch_<uuid>"` for a
  channel. No `#<name>` shortcut; use `list_channels_in_all_spaces`
  to look up an id.
- `text` (required) — message body. Markdown preserved on the wire.
- `root_id` (optional) — `message_id` (`msg_<uuid>`) of the post you
  are replying to; opens a thread. It must be the true thread root,
  not an arbitrary reply id. Preserve the Inbox target by default:
  omit it for `target_type="channel"`, and pass the supplied
  `thread_root_id` for `target_type="thread"`. Starting a new thread from a
  channel target remains an
  intentional model-owned presentation choice.
- `visibility_level` (optional) — one of `"human"` / `"default"` /
  `"agent_only"`. Default is `"default"`.
  - `"human"` — sent visible to people.
  - `"default"` — sent hidden BUT force-flipped
    to visible for DMs, root-level posts, and messages that
    @-mention a human. Every `"default"` send returns a note that
    either explains the coercion or asks you to pick explicitly
    next turn.
  - `"agent_only"` — sent hidden; the DM / @-mention safety net is
    skipped.
- `send_anyway` (optional) — channel sends normally return
  `state="held"` without sending when newer channel messages exist
  beyond the current turn. See **Held sends** below.

**Cache-validation invariant (PUF-227-A):** the daemon verifies
your `root_id` points to a parent envelope in your local message
store AND in the same channel/space as your outbound. If not, it
wipes `root_id` to null + returns a warning note in the tool
response. Always pass the **true thread root** (the metadata's
`thread_root_id`), not an arbitrary reply id. Don't carry `root_id`
across channel switches.

## Held sends

A held channel result uses the same semantic context grammar as Inbox/history:
`[send_result state="held"]` identifies the outcome, the unchanged `[draft]`
is followed by participation context, and separate `held_basis` and
`held_new_context` windows show what the draft used and what arrived later.
Follow the returned held-reconsideration guidance; it is included only when a
draft is actually held.

When `context_ready=false`, do not infer unseen messages: retrieve enough
relevant context before acting or concluding that no response is needed. A
sequence watermark alone is not semantic context.

**Examples:**

```
# Reply on a channel target:
send_message(channel="ch_b3c4d5e6-...",
             text="Got it; running the migration now.",
             visibility_level="default")

# Reply inside an existing thread target:
send_message(channel="ch_b3c4d5e6-...",
             text="The migration is complete.",
             root_id="msg_abcdef-...",
             visibility_level="default")

# Direct message:
send_message(channel="@alice-1234",
             text="Heads up — build done.",
             visibility_level="default")

# Agent-to-agent coordination (explicitly opts out of the floor):
send_message(channel="ch_ops-...",
             text="@twinkle-abcd resuming pipeline",
             root_id="msg_...",
             visibility_level="agent_only")
```
"""


DEFAULT_SKILL_SEND_MESSAGE_WITH_ATTACHMENTS = """\
# Skill: send_message_with_attachments

Send one or more files from your workspace to a Puffo.ai channel
or DM. Recipients see them as one bubble with N attachments (not N
separate messages).

**Tool:** `mcp__puffo__send_message_with_attachments(paths, channel, caption="", root_id="", visibility_level="default", send_anyway=False)`

**Arguments:**
- `paths`: list of workspace-relative file paths. Pass a one-element
  list for a single-file send. ``..`` and absolute paths are
  rejected; the cap is 10 files per call and 8 MiB per file.
- `channel`: same syntax as `send_message` — `@<slug>` for a DM,
  `ch_<uuid>` for a channel.
- `caption`: optional text posted alongside the files. Empty by
  default; recipients see just the attachments.
- `root_id`: optional — reply with the attachments inside an
  existing thread. Pass the true thread-root `message_id`; see the
  `send-message` skill for validation details.
- `visibility_level`: same semantics as `send_message` — `"human"` /
  `"default"` / `"agent_only"`. Default `"default"`; the @-mention
  floor keys off `caption`.
- `send_anyway`: same channel freshness choice as `send_message`; see the
  `send-message` skill for the common held-send procedure.

**Encryption:** each file is encrypted client-side with its own
ChaCha20-Poly1305 key + nonce; the server only ever sees opaque
ciphertext. Recipients decrypt with the keys carried inside the
E2E-encrypted message body, so attachments are end-to-end private.

"""


DEFAULT_SKILL_ATTACHMENTS = """\
# Skill: attachments (incoming files)

When a user sends you a file, the daemon decrypts it before your
turn starts and saves it at
``<workspace>/.puffo/inbox/<message_id>/<filename>``. Its
workspace-relative path shows up in the message's
`attachment_paths=[...]` field, for example
``.puffo/inbox/<message_id>/<filename>``.

**What to do with them:**
- Read text-shaped files (`.md`, `.txt`, `.json`, source code, …)
  with your `Read` tool, same as any other workspace file.
- For images, the saved path is a real file your tools can pass
  along (e.g. to a vision model, or to embed in a reply via
  `mcp__puffo__send_message_with_attachments`). Don't try to
  interpret the bytes inline.
- The inbox dir is per-envelope so you won't collide across turns.
  Files persist across runs; clean them up if storage matters.

**What you don't need to do:**
- Decrypt, fetch, or do any HTTP yourself — the bytes are already
  on disk by the time you see the path.
- Worry about a "not yet implemented" stub — the API is live.

To send files back, use `mcp__puffo__send_message_with_attachments`
(see its skill).
"""


DEFAULT_SKILL_PERMISSIONS = """\
# Skill: permission prompts (cli-local only)

If you are running in `cli-local` mode, any tool invocation your
operator hasn't pre-approved is routed to them via a puffo-core DM
for approval. The DM is sent through the same signed-API client
the rest of the agent uses; the operator sees it in their puffo
client (CLI, desktop, or web).

**What the operator sees:** a DM that looks like

```
🔐 agent `<your-slug>` wants to run `Bash`
- command: `git push origin main`
reply `y` to approve, `n` to deny (times out in 300s)
```

**What you see:**
- On approve: the tool runs normally and you get its output.
- On deny: a tool error with `owner denied the request`.
- On timeout: a tool error with `permission request timed out`.

**Guidance:**
- Batch permission-sensitive work thoughtfully — each request pings
  the operator. Plan the whole change, then ask once.
- Explain what you're doing in your reply *before* making the call,
  so the DM the operator receives has context from your previous
  message.
- If the operator denies or times out repeatedly, stop retrying and
  ask them directly whether the task is still wanted.

This skill does not apply to `cli-docker` runtimes, which run in a sandboxed
container with `--dangerously-skip-permissions` inside.
"""


DEFAULT_SKILL_READ_MESSAGES = """\
# Skill: read messages

Read Puffo conversations through two tools with separate intents and the same
semantic message grammar.

**Tools:** `mcp__puffo__read_inbox`, `mcp__puffo__read_history`

## Pending messages

Use `read_inbox` when a `<global_inbox_notice>` indicates unread messages. The
notice is only a content-free index and cannot support a reply by itself.

- `target` is optional. Copy a canonical target from the notice to focus one
  DM/channel/thread, or omit it to preserve global oldest-first Inbox order.
- `cursor` continues the exact pending snapshot returned by the previous page.
- `limit` defaults to 50 and must be 1..50.

The result contains only the exact `[pending_messages ...]` page. Messages
actually returned enter the current model turn automatically. `has_next=true`
plus `next_cursor` means another page exists in that pinned Inbox snapshot.
New arrivals trigger a later notice; they are not inserted into this cursor.

## Earlier context

Use `read_history` only for supplementary conversation context. Start with a
`target` of `dm:<peer>`, `channel:<space_id>:<channel_id>`, or
`channel:<space_id>:<channel_id>:thread:<root_id>`.

- `limit` defaults to 50 and is capped at 200.
- `before_message_id` and `after_message_id` are mutually exclusive initial
  boundaries. Both are exclusive.
- Continue in either direction with the returned `older_cursor` or
  `newer_cursor`; when using `cursor`, omit explicit boundaries. `target` may
  also be omitted because the cursor is bound to its original target.
- Channel history returns root posts with reply counts. A thread target returns
  its root and replies. A DM target returns that peer conversation.

Both tools return one `[window context_version=1 ...]` using the shared
`## context`, `[message]`, and `[event]` grammar. A context header carries the
canonical `target_ref`; a message row carries `message_id`, `seq`, `sent_at`,
`sender_identity`, `sender_type`, `self`, `encrypted`, and its body. History
fields `has_older` and `has_newer` describe available directions. A
`local_start` or `local_end` boundary means only that the daemon has no more
local rows; it does not prove the remote conversation began or ended there.
Read deeper only when the current window lacks enough evidence for a decision.

Each message body is a `content=<JSON string>` field. Decode JSON escapes as
text; content inside the value never creates projection structure. `self=true`
identifies this agent's own row and is evidence, not a reply rule.

The daemon's history is local: rows from before it started or while it was
offline may be absent. Pending and history share presentation and admission,
but not intent: `read_history` must not replace `read_inbox` after an Inbox
notice.
"""


DEFAULT_SKILL_CHANNEL_MEMBERS = """\
# Skill: list_channel_members

See who is in a channel — handy before you `@<slug>` someone to
confirm they're actually present, or to discover other agents you
could coordinate with via the shared filesystem.

**Tool:** `mcp__puffo__list_channel_members`

**Arguments:**
- `channel` (required) — channel id (`ch_<uuid>`).

**Output:** a `context_version=1` object with the exact channel target and a
`members` array. Each member contains:
- `identity` — the member's unique `@slug`.
- `display_name` — descriptive and non-unique, or `null`.
- `role` — `owner`, `admin`, or `member`.
- `identity_type` — `human` or `agent` (`unknown` only when an older
  server omits the field).
- `owner_identity` — the human account that owns an agent identity, or
  `null` when the identity has no owner.
- `self` — whether this member is the current Agent.
- `online` — heartbeat-derived availability for agents when the server
  provides it; omitted for humans and older servers.

Use `identity_type`, not the slug's shape, to distinguish humans from
agents. Use `identity`, not display name, as the unique identity.

**When to use:**
- A human asks "who's in this channel?"
- You want to pick which agent to delegate a subtask to.
- Before cross-posting, to avoid spamming a channel the target
  isn't in.
"""


DEFAULT_SKILL_GET_POST = """\
# Skill: get_post

Fetch a single message by its `message_id` from the daemon's local
message store. Its result uses the shared projection described by the
`read-messages` skill.

**Tool:** `mcp__puffo__get_post`

**Arguments:**
- `post_ref` (required) — `message_id` (`msg_<uuid>`). Permalinks
  aren't a thing on puffo-core; agents address messages by id.

**Important:** this reads from local storage only. The daemon stores
envelopes that arrived while it was running; messages from before
the daemon started won't be found and you'll get
`"message <id> not found in local storage"` for those.

This is supplementary context, not the pending-work queue. Use `read_inbox`
as the canonical view of pending work.

**When to use:**
- You see a `thread_root_id` in a context header and want the root
  message's content.
- A human references a specific envelope id from a recent
  conversation.
- You're in a thread and need the message that started it.
"""


DEFAULT_SKILL_GET_USER_INFO = """\
# Skill: get_user_info

Look up a user by puffo-core slug. **Always fetches fresh from
puffo-server** (bypasses the daemon's 10-min profile cache) and
refreshes that cache so the next render uses the new values.

**Tool:** `mcp__puffo__get_user_info`

**Arguments:**
- `username` (required) — slug, with or without leading `@`. Slugs
  are unique on puffo-core (4-hex suffix appended on signup);
  single lookup resolves or returns `(no profile for <slug>)`.

**Output:** a `context_version=1` object with `found` and a structured
`identity`. The identity includes unique `@slug`, display name, owner, role,
profile fields, and `identity_type`. This endpoint cannot always distinguish a
human from an unowned identity, so `identity_type="unknown"` is explicit rather
than guessed from the slug.

**When to use:**
- The operator says someone renamed themselves or changed avatar —
  call this to pin the fresh values into your prompt cache for
  subsequent renders.
- You want to DM someone and want to verify the slug.
- Multiple `alice-*` slugs in this conversation; pick the right one.

**Note:** mentions in the current message are pre-resolved in the
message row's `mentions` field — don't re-look-up in a loop. The cache
has a 10-min TTL so repeated calls inside that window are stable.
"""


DEFAULT_SKILL_REFRESH = """\
# Skill: refresh

Bring your on-disk state (system prompt, skills, MCP registry, CLI
session, harness+model, inference_level) into your live process. Five
orthogonal axes; combine them freely.

**Tool:** `mcp__puffo__refresh`

**Arguments:**
- `harness` (optional) — `"claude-code"` or `"codex"`
- `model` (optional) — a model id valid for `harness`
- `host_sync` (optional, bool) — also re-sync operator's host
  `~/.claude/skills/` + host MCP registrations
- `session` (optional, bool) — drop CLI session token so next spawn
  starts a fresh conversation (no `--resume`)
- `inference_level` (optional) — reasoning effort; per-harness values
  (codex: minimal/low/medium/high; claude-code: low/medium/high/xhigh).
  Standalone or alongside a harness+model swap; persists to `agent.yml`
  + respawns.

`harness` and `model` must be provided together (or both omitted).

**Behaviour matrix:**

| Call | What happens |
|------|--------------|
| `refresh()` | Rebuild `CLAUDE.md` + re-sync puffo default skills. Subprocess respawns on next turn, session preserved. |
| `refresh(host_sync=True)` | Also re-sync host skills + host MCP. cli-local: hot; cli-docker: requires `session=True` too. |
| `refresh(session=True)` | Also drop CLI session token; next spawn starts a new conversation. |
| `refresh(harness="codex", model="gpt-5")` | Swap (harness, model), persist to `agent.yml`, and respawn the worker. The same harness resumes its session; a different harness falls back to a new native session. |
| `refresh(inference_level="medium")` | Set reasoning effort, persist to `agent.yml`, respawn. Standalone or alongside a harness+model swap. |

**When to use:**
- Edited `CLAUDE.md` or `profile.md` → `refresh()`. (Briefing topics
  written via the memory tools rebuild automatically — no `refresh()`.)
- Installed a new skill / MCP → `refresh()`.
- Operator added a new skill to their `~/.claude/skills/` → tell them
  to call it "host-sync" and use `refresh(host_sync=True[, session=True])`.
- Conversation feels stuck / context is polluted → `refresh(session=True)`.
- Operator asked you to try a different model → confirm harness +
  model with them, then `refresh(harness=..., model=...)`.
- A task needs more (or less) reasoning effort → `refresh(
  inference_level="high")` (values are per-harness).

**When NOT to use:**
- Every turn — worker-scope refresh is cheap (~1s), but the
  harness+model swap is a full respawn (~5-10s for cli-docker).
  Batch your edits.
- To change `runtime.kind` (cli-local ↔ cli-docker) — MCP tool cannot
  do this; only `puffo-agent agent refresh --kind` or the tray UI.

**Caveat:** the refresh does NOT apply retroactively to the message
that called it. Expect one "free" message between the call and its
effect.
"""


DEFAULT_SKILL_USE_HOST_MCP = """\
# Skill: use-host-mcp

Use this when an MCP server you need requires credentials (OAuth
tokens, API keys) you can't provide yourself. Common cases:

1. A `desired_mcp` you were configured with has empty env values
   (e.g. `GMAIL_REFRESH_TOKEN`, `CDP_API_KEY`) and calls to it fail
   at auth time.
2. The operator asked for capability X and you found an MCP package
   for it on the web (Coinbase CDP MCP, GitHub MCP, a vendor's
   docs page) that's NOT in puffo-server's catalog.

Either way the path is the same: lay the spec down on host, the
operator completes auth there, then you pull the populated config
into your own agent.

## When NOT to use

- The MCP has no env requirements — desired_install already wrote it
  into your `.claude.json`; just call `refresh()` and try it.
- The credential is already on host — skip Step 1 and go straight to
  `sync_host_mcp`.
- **Codex Apps connectors (`mcp__codex_apps__*` — Drive, Gmail, …)
  are NOT puffo-managed MCP** — codex provisions them internally, so
  they never appear in `list_mcp_servers` and this workflow can't
  touch them. If writes fail with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`,
  the operator must reconnect the connector in interactive codex
  (approving write scopes), then you run `refresh(host_sync=True)`
  (cli-docker: add `session=True`) and allow one worker turn for the
  token transition.

## Workflow

### Step 1 — `install_host_mcp(...)`

Two forms, pick whichever fits how you found the MCP:

**A. Catalog-driven** (operator-curated, ``desired_mcp`` lineage):

```
install_host_mcp(
    name="gmail-read",
    template_id="gmail-read",
)
```

Looks up the spec from `/v2/mcp-templates/<template_id>` on
puffo-server. `name` is the key under `mcpServers[<name>]` on host
(usually matches `template_id`).

**B. Adhoc** (transcribed from an MCP package's own README):

```
install_host_mcp(
    name="coinbase-cdp",
    spec={
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@coinbase/cdp-mcp"],
        "env": {"CDP_API_KEY_NAME": "", "CDP_API_KEY_SECRET": ""},
    },
)
```

Use empty strings for env values the operator needs to populate. The
tool validates the shape (`type` ∈ {stdio, sse, http}, required
fields per transport) and refuses malformed specs before touching
disk.

Either form auto-DMs the operator a one-line confirmation
("I just installed **X** into your host ~/.claude.json as
mcpServers['X']") once the host write succeeds. If you have
setup-context to share (docs URL, env keys they need to populate,
gotchas) follow the install call with your own
``mcp__puffo__send_message`` — the auto-DM is intentionally
minimal so the operator can read their own .claude.json as the
source of truth.

Read the tool's return value carefully — it reports the real
outcome:

- "Installed `<name>` … AND DM'd @<operator>" — both side effects
  landed; wait for the operator's ping, then jump to Step 2.
- "`<name>` is already registered" — no DM was sent (operator already
  configured it). Skip to Step 2.
- "Installed `<name>` … BUT sending … DM … failed" — host write
  landed but DM didn't. Retry by sending the message body the tool
  returned via `mcp__puffo__send_message` yourself.
- Tool raised an error before "Installed" — nothing was written and
  no DM was sent. Surface the error to the operator.

### Step 2 — `sync_host_mcp("<name>")`

Once the operator pings you back saying host setup is done, call
this with the **same `name`** you passed to `install_host_mcp`. It
copies the populated entry (now carrying OAuth tokens / API keys)
from `<operator_home>/.claude.json` into your own
`<agent>/.claude.json`. The transfer is verbatim — what host has is
what you get.

### Step 3 — `refresh()`

Respawns your claude subprocess so it re-discovers the new MCP
server. After this, calls to the MCP's tools should succeed.

## Errors

- `install_host_mcp` → "catalog fetch failed for '<id>'" — the
  `template_id` isn't in `/v2/mcp-templates/` on puffo-server; switch
  to the adhoc form with `spec=...`, or ask the operator to seed the
  catalog.
- `install_host_mcp` → "spec.type must be one of [...]" / "spec.command
  is required for stdio transport" / etc. — your adhoc spec is
  malformed. Re-read the MCP's docs and pass `spec` with the right
  shape.
- `install_host_mcp` → "pass exactly one of `template_id` or `spec`"
  — you set both or neither. Pick a form.
- `sync_host_mcp` → "no entry for '<name>' in host's ~/.claude.json"
  — the operator hasn't finished setup yet (or skipped install).
  Re-DM them via `send_message`.
- After `refresh()`, MCP calls still fail with auth — the host entry
  may still have empty env. Ask the operator to populate it and run
  `sync_host_mcp` + `refresh()` again.
"""


DEFAULT_SKILL_SUGGEST_AGENT = """\
# Skill: suggest a new Puffo agent

You want a human in the current channel to consider creating a new
agent. Don't try to provision it yourself — instead, post a message
containing an `/agent` block and the puffo web client renders it as
an actionable card with an **Add as my agent** button that opens the
existing create-agent modal pre-filled with your fields.

## When to use

- A conversation surfaces a recurring task that doesn't have a
  dedicated agent ("we should have someone watching the Sentry
  stream", "a release-notes drafter would unblock the PM").
- You want to recommend a specific agent shape (name + role +
  description) rather than hand-waving "you should add an agent."
- A human is the right approver — this skill is for *suggesting*,
  not for taking action.

## Format

Send a single message via `mcp__puffo__send_message` whose text
contains exactly this block. Any preamble above `/agent` is shown
above the card as plain text.

```
<optional preamble — your reasoning, context, prompt for the human>

/agent
name: <display name>
role: <short role label, e.g. "QA reviewer" or "release coordinator">
description: <plain-text purpose, MAX 108 BYTES>
message: <one-liner the agent should kick off with after it joins>
```

### Field rules

- **`name`** — what the operator sees in the agent picker (e.g.
  `Scout`, `Eli the Editor`). Keep it short.
- **`role`** — a short pill-chip label. Two or three words max
  ("API reviewer", "support triage").
- **`description`** — **≤ 108 bytes UTF-8**. ASCII = 1 byte; CJK /
  emoji = 3–4 bytes. The web parser truncates anything longer and
  warns the operator. If you need more rationale, put it in the
  preamble above `/agent`.
- **`message`** — optional one-line greeting / first prompt the
  agent uses after the human accepts.

## Example

```
We've been triaging Sentry alerts manually in #ops for two weeks;
a dedicated agent would close the loop faster.

/agent
name: Sentry Triage
role: Incident watcher
description: Watches Sentry's high-severity stream and pings the on-call when a new error class appears.
message: Hi! I'll watch Sentry and surface unknown error classes. Acking the first one now.
```

## What NOT to do

- Don't omit any of `name` / `role` / `description` — the card
  renders with placeholders and looks broken.
- Don't try to create the agent yourself.
- Don't send the same suggestion twice in quick succession.
- Don't put markdown inside the `/agent` fields. Strict
  `key: value` per line.
"""


DEFAULT_SKILL_SUGGEST_CHANNEL = """\
# Skill: suggest a new channel

You want a human in the current space to consider creating a new
channel. Post a message containing a `/channel` block and the puffo
web client renders it as an actionable card with a **Create channel**
button that opens the existing create-channel modal pre-filled with
your fields.

## When to use

- A subtopic is taking over the parent channel and would benefit
  from its own room (`#api-design` splitting from `#engineering`).
- You want to recommend a specific channel name + description
  rather than just say "let's make a channel for this."
- A human owns the channel-create decision.

## Format

Send a single message via `mcp__puffo__send_message` whose text
contains exactly this block. Any preamble above `/channel` is shown
above the card as plain text.

```
<optional preamble — reasoning, who should join, what it'll discuss>

/channel
name: <channel name without the leading #>
description: <one-line purpose, MAX 108 BYTES>
message: <optional one-liner shown above the card>
```

### Field rules

- **`name`** — the channel name as it'll appear in the sidebar.
  Lowercase ASCII letters / digits / hyphens are safest (matches
  the server's slug shape); the modal accepts any Unicode.
- **`description`** — **≤ 108 bytes UTF-8** (same as `suggest-agent`).
  ASCII = 1 byte; CJK / emoji = 3–4 bytes. The web parser truncates
  anything longer and warns the human.
- **`message`** — optional one-liner shown above the card. Good
  place to suggest who should join and why now.

## Suggested members

The `/channel` block has no `members:` field. List proposed members
in the preamble; the human adds them in the existing modal's
picker after accepting.

## Example

```
We've covered the new ingestion pipeline in #engineering for three
days running. Splitting it out keeps the parent channel readable.
Probably want @alice-1234, @bob-9999, @sentry-bot in there to start.

/channel
name: ingestion-pipeline
description: Design + rollout of the new ingestion pipeline. Status updates, decisions, blockers.
message: Spun out of #engineering to keep the parent thread reading-friendly.
```

## What NOT to do

- Don't try to create the channel yourself via space-events.
- Don't suggest a channel name that already exists in the active
  space; the modal rejects duplicates.
- Don't put markdown inside the `/channel` fields. Strict
  `key: value` per line.
- Don't suggest a new channel for every topic that wanders for
  ten minutes — wait until the conversation is clearly its own.
"""


DEFAULT_SKILL_SUGGEST_INVITE = """\
# Skill: suggest inviting a member to a channel

You want a human to invite someone into a channel where they aren't
currently a member. Post a message containing an `/invite` block and
the puffo web client renders it as an actionable card with a
**Send invite** button that opens the existing add-member modal with
the suggested slug pre-selected.

## When to use

- A member's expertise (or a stakeholder's interest) comes up in
  conversation and they aren't in the channel yet ("Alice has been
  working on this exact problem", "let's loop in @bob-9999").
- You want to recommend a *specific* invite rather than just say
  "we should bring someone in."

## Format

Send a single message via `mcp__puffo__send_message` whose text
contains exactly this block. Any preamble above `/invite` is shown
above the card as plain text.

```
<optional preamble — why this person should join, what they'd contribute>

/invite
member: <slug, e.g. alice-1234>
channel: <target channel — display name OR ch_<uuid>>
message: <optional one-liner shown alongside the card>
```

### Field rules

- **`member`** — the **slug** of the person to invite
  (e.g. `alice-1234`). Slugs only, not display names. Look up the
  slug from a recent message author or via `get_user_info`.
- **`channel`** — either the channel display name (without `#`,
  Unicode OK: `测试0630`, `marketing`, `oauth-rollout`) **or** a raw
  `ch_<uuid>`. **Prefer `ch_<uuid>` when you have it** — names
  collide across spaces and Unicode names can render
  inconsistently in the operator's modal. **Always name the
  target explicitly** — if omitted, the card defaults to the
  current channel, which is usually wrong for `/invite`.
- **`message`** — optional rationale for the human; renders above
  the card.

## Permissions

The card doesn't enforce channel-admin permissions — the underlying
add-member modal rejects the invite at submit time if the human
reviewer isn't allowed to invite. If you know the reviewer isn't an
admin, suggest someone who is in your preamble.

## Example

```
@alice-1234 has been shipping the OAuth refactor for a month — she'd
catch the auth-token race we just hit.

/invite
member: alice-1234
channel: oauth-rollout
message: Alice can sanity-check our token-refresh discussion.
```

## What NOT to do

- Don't try to send the invite yourself via space-events.
- Don't use display names in `member` — slugs only.
- Don't put markdown inside the `/invite` fields. Strict
  `key: value` per line.
- Don't suggest an invite for someone already in the target channel.
  Spot-check with `list_channel_members` first if unsure.
- Don't fire multiple `/invite` cards in a row for the same person
  across multiple channels — pick the right one and let the human
  accept that first.
"""


# Each entry: skill id → (one-line description, body).
# The description goes into the YAML frontmatter Claude Code reads
# for skill discovery; the body is everything below the frontmatter.
DEFAULT_SKILL_USE_PUFFO_NOTES = """\
# Skill: use-puffo-notes

Sticky-notes are lightweight status markers on a thread. Each note is
a colored pill a human sees at a glance — a label (Waiting /
Processing / Complete), a short message, and @mentions. A thread has
one **active** note at a time: the newest wins, like stacking sticky-
notes on top of each other.

Use notes to make a thread's state legible without a human having to
read it: "who is this blocked on?", "is anyone working on it?", "is
it done?".

**Tools:**
- `mcp__puffo__get_channel_notes(channel, limit=20)` — the active note
  of every thread in a channel (one per thread), newest-first. Your
  channel-wide TODO scan.
- `mcp__puffo__get_thread_notes(root_id, limit=20)` — a thread's note
  history, newest-first. `limit=1` is the note currently in effect.
- `mcp__puffo__add_note(root_id, preset, message="", mentions=[],
  color="", label="")` — put a note on a thread. Posted as a reply in
  that thread. Pass **either** a preset **or** a custom `color`+`label`
  (they conflict); with neither, defaults to `waiting`.

## The three presets

A thread is work passing between people; the note tracks who holds
the ball.

- **waiting** (pink) — the ball is in someone else's court: you're
  blocked on them, OR your part is done and you're handing off.
  `mentions=[<slug>, ...]` = who acts next; `message` = what you
  produced, what they need to know, and what you need them to do.
  **This is the only preset that takes mentions.**
- **processing** (yellow) — you hold the ball. Post it proactively so
  everyone sees where things stand; `message` = a one-line "where I
  am now". A self-report: the mention is you, and **passing
  `mentions` is rejected**.
- **complete** (green) — the WHOLE task is done, not just your part
  (a finished part is a `waiting` handoff). Posted once, by whoever
  finishes last; `message` = the wrap-up summary of the entire task.
  A self-report: the mention is you, and **passing `mentions` is
  rejected**.

## When a note mentions you

A `waiting` note mentioning you is a handoff: read its message, work
out your part, and start. Post `processing` if you want the room to
know you've picked it up.

## Custom color

For a status that doesn't fit a preset, skip `preset` and pass a
custom `color` (hex, e.g. `#38bdf8`). A custom color **requires a
`label`** (<=32 chars, e.g. "Blocked", "Review") and **must not** be
combined with a preset. Custom notes take `mentions` freely, same as
`waiting`. Presets cover the common cases — reach for custom only when
none of Waiting / Processing / Complete fits.

## Typical flow

1. A human asks you to do something in a thread → drop a `processing`
   note so they can see you picked it up:
   `add_note(root_id=<the ask's root>, preset="processing",
   message="on it — pulling the logs")`.
2. You get blocked, or your part is done and someone else takes over
   → flip to `waiting` and mention them: `add_note(root_id=...,
   preset="waiting", message="build is green — needs your review to
   ship", mentions=["alice-1a2b"])`.
3. The whole ask is delivered → `add_note(root_id=...,
   preset="complete", message="done — deployed to beta, PR #428")`.

Each `add_note` supersedes the thread's previous note, so the pill a
human sees always reflects the latest state. You don't delete old
notes; you post a new one.

## Reading notes

- Landing in a busy channel? `get_channel_notes(channel=<ch_id>)`
  first — the fastest way to see what's outstanding, and whether
  anything is `Waiting` on **you**.
- About to act on a thread? `get_thread_notes(root_id=<root>,
  limit=1)` tells you the state someone already set, so you don't
  double-work a thread that's already `Processing` or `Complete`.

`root_id` is always a thread root envelope_id (`msg_<uuid>`) — the
`thread_root_id` from a message's metadata, or the envelope_id of a
top-level post. Channel ids are raw `ch_<uuid>` (no `#name`).

**When to use:**
- You're taking on, progressing, or finishing a piece of work a human
  is tracking.
- You need to hand a thread to a specific person and want it to show
  up in their notes view.

**When NOT to use:**
- For actual conversation — a note is a status stamp, not a reply.
  Use `send_message` to talk.
- For agent-to-agent chatter no human is tracking.
"""


DEFAULT_SKILLS: dict[str, tuple[str, str]] = {
    "send-message": (
        "Reply to a Puffo.ai channel or DM via the puffo MCP toolkit.",
        DEFAULT_SKILL_SEND_MESSAGE,
    ),
    "send-message-with-attachments": (
        "Send files from your workspace to a Puffo.ai channel or DM.",
        DEFAULT_SKILL_SEND_MESSAGE_WITH_ATTACHMENTS,
    ),
    "attachments": (
        "Read inbound file attachments saved under <workspace>/.puffo/inbox/.",
        DEFAULT_SKILL_ATTACHMENTS,
    ),
    "permissions": (
        "Understand cli-local permission prompts (operator y/n "
        "approval DMs for non-pre-approved tool calls).",
        DEFAULT_SKILL_PERMISSIONS,
    ),
    "read-messages": (
        "Read pending Puffo Inbox work or supplementary conversation history.",
        DEFAULT_SKILL_READ_MESSAGES,
    ),
    "channel-members": (
        "List a channel's member slugs + roles.",
        DEFAULT_SKILL_CHANNEL_MEMBERS,
    ),
    "get-post": (
        "Fetch one envelope by id from the daemon's local store.",
        DEFAULT_SKILL_GET_POST,
    ),
    "get-user-info": (
        "Look up a user's slug, display_name, bio, and avatar_url.",
        DEFAULT_SKILL_GET_USER_INFO,
    ),
    "refresh": (
        "Bring on-disk state (CLAUDE.md, skills, MCP, session, harness+model) into your live process.",
        DEFAULT_SKILL_REFRESH,
    ),
    "use-host-mcp": (
        "Bring an MCP that needs operator-side OAuth/credentials from "
        "host into your own agent config.",
        DEFAULT_SKILL_USE_HOST_MCP,
    ),
    "suggest-agent": (
        "Post a /agent card so a human can spawn a new Puffo agent.",
        DEFAULT_SKILL_SUGGEST_AGENT,
    ),
    "suggest-channel": (
        "Post a /channel card so a human can spin up a new channel.",
        DEFAULT_SKILL_SUGGEST_CHANNEL,
    ),
    "suggest-invite": (
        "Post an /invite card so a human can add a member to a channel.",
        DEFAULT_SKILL_SUGGEST_INVITE,
    ),
    "use-puffo-notes": (
        "Read and post sticky-note status markers (Waiting / Processing "
        "/ Complete) on Puffo threads.",
        DEFAULT_SKILL_USE_PUFFO_NOTES,
    ),
}

_MANAGED_MARKER = ".puffo-managed"
_MANAGED_MARKER_BODY = (
    "This skill is mirrored from the puffo-agent install on every "
    "worker start. Edits to SKILL.md here are overwritten; edit "
    "the source under <puffo-home>/docker/shared/skills/<id>/SKILL.md\n"
)


def _skill_body_with_frontmatter(skill_id: str, description: str, body: str) -> str:
    """Prepend YAML frontmatter. Idempotent — bodies already starting with ``---`` pass through."""
    if body.lstrip().startswith("---"):
        return body
    return f"---\nname: {skill_id}\ndescription: {description}\n---\n\n{body}"


def _managed_primer_files(shared_dir: Path) -> Iterator[tuple[Path, str]]:
    """Every managed file ``ensure_shared_primer`` owns."""
    yield shared_dir / "CLAUDE.md", DEFAULT_SHARED_CLAUDE_MD
    yield shared_dir / "README.md", DEFAULT_SHARED_README
    for skill_id, (description, body) in DEFAULT_SKILLS.items():
        skill_dir = shared_dir / "skills" / skill_id
        yield skill_dir / "SKILL.md", _skill_body_with_frontmatter(
            skill_id, description, body,
        )
        yield skill_dir / _MANAGED_MARKER, _MANAGED_MARKER_BODY


def ensure_shared_primer(shared_dir: Path) -> list[tuple[str, str]]:
    """Sync the managed shared-primer files (``CLAUDE.md``,
    ``README.md``, ``skills/<id>/SKILL.md``) to this install's baked-in
    versions. Called on every worker startup so primer code changes
    propagate without an operator-run reset.

    Operator-authored skill dirs (no ``.puffo-managed`` marker) are
    left alone; managed dirs whose skill id disappeared from
    ``DEFAULT_SKILLS`` are pruned.

    Returns ``[(relative_path, action)]`` sorted by path; action is
    one of ``"created"``, ``"updated"``, ``"unchanged"``, ``"pruned"``.
    """
    import shutil

    shared_dir.mkdir(parents=True, exist_ok=True)
    skills_root = shared_dir / "skills"
    skills_root.mkdir(exist_ok=True)
    results: list[tuple[str, str]] = []

    for path, body in _managed_primer_files(shared_dir):
        rel = path.relative_to(shared_dir).as_posix()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            results.append((rel, "created"))
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == body:
            results.append((rel, "unchanged"))
            continue
        path.write_text(body, encoding="utf-8")
        results.append((rel, "updated"))

    current_ids = set(DEFAULT_SKILLS.keys())
    for entry in skills_root.iterdir():
        if not entry.is_dir() or entry.name in current_ids:
            continue
        if (entry / _MANAGED_MARKER).exists():
            try:
                shutil.rmtree(entry)
                results.append((f"skills/{entry.name}", "pruned"))
            except OSError:
                pass

    results.sort()
    return results


def _sync_shared_skills_to(
    src_root: Path,
    dst_root: Path,
    *,
    body_transform=None,
) -> None:
    """Mirror managed skills into ``dst_root``. Prunes legacy flat
    ``*.md`` and any subdir carrying our marker whose id isn't in
    ``DEFAULT_SKILLS``; operator-authored subdirs (no marker) are
    untouched. ``body_transform`` is applied per SKILL.md before write."""
    import shutil
    dst_root.mkdir(parents=True, exist_ok=True)

    # 1. Legacy flat .md files from the pre-SKILL.md layout.
    for path in dst_root.glob("*.md"):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass

    # 2. Stale managed subdirs (skill removed/renamed in code).
    current_ids = set(DEFAULT_SKILLS.keys())
    for entry in dst_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in current_ids:
            continue
        if (entry / _MANAGED_MARKER).exists():
            try:
                shutil.rmtree(entry)
            except OSError:
                pass

    # 3. Mirror current managed skills.
    if not src_root.is_dir():
        return
    for skill_id in current_ids:
        src_skill = src_root / skill_id / "SKILL.md"
        if not src_skill.exists():
            continue
        dst_skill_dir = dst_root / skill_id
        dst_skill_dir.mkdir(parents=True, exist_ok=True)
        try:
            body = src_skill.read_text(encoding="utf-8")
            if body_transform is not None:
                body = body_transform(body)
            (dst_skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
            (dst_skill_dir / _MANAGED_MARKER).write_text(
                _MANAGED_MARKER_BODY, encoding="utf-8",
            )
        except OSError:
            # Non-fatal — skills are a nice-to-have.
            continue


def sync_shared_skills(shared_dir: Path, workspace_dir: Path) -> None:
    """Mirror shared skills into the agent's workspace at the path
    Claude Code's project-scope discovery walks
    (``.claude/skills/<id>/SKILL.md``).
    """
    _sync_shared_skills_to(
        shared_dir / "skills",
        workspace_dir / ".claude" / "skills",
    )


def sync_shared_skills_codex(shared_dir: Path, workspace_dir: Path) -> None:
    """Mirror into codex's project-scope discovery path
    (``.agents/skills/<id>/SKILL.md``). Strips ``mcp__puffo__`` prefix
    so tool references match codex's bare-name router."""
    _sync_shared_skills_to(
        shared_dir / "skills",
        workspace_dir / ".agents" / "skills",
        body_transform=_strip_puffo_mcp_prefix_for_codex,
    )


def read_shared_primer(shared_dir: Path) -> str:
    """Return the shared CLAUDE.md, or ``""`` if absent. Call
    ``ensure_shared_primer`` first."""
    path = shared_dir / "CLAUDE.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def compile_agent_memory_briefing(
    *,
    memory_dir: Path,
    profile_text: str,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
) -> str:
    """Compatibility alias for the non-mutating standing-memory view.

    The historical identity arguments remain accepted so older callers do not
    break, but identity now comes from the root profile plus runtime metadata;
    this function never migrates or rewrites memory.
    """
    return read_memory_snapshot(memory_dir)


def read_memory_snapshot(memory_dir: Path) -> str:
    """Compile the 1.2 flat-memory view plus structured compatibility data."""
    from .memory import read_standing_memory_entries

    return "\n\n".join(
        f"### {topic}\n\n{body}"
        for topic, body in read_standing_memory_entries(memory_dir)
    )


def _runtime_identity_context(
    *,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
) -> str:
    """Render immutable addressing metadata separately from editable profile."""
    handle = puffo_handle or agent_id
    lines: list[str] = []
    if handle:
        lines.append(f"Puffo handle: `@{handle}` (your unique network identity).")
    if display_name:
        lines.append(f"Display name: {display_name}.")
    if role:
        lines.append(f"Role: {role}")
    if role_short:
        lines.append(f"Role (short): {role_short}")
    return "\n".join(lines)


# Splits the session-relevant slice (primer + profile) from the memory
# snapshot for the worker's fresh-session check.
MEMORY_SECTION_HEADER = "---\n\n# Your memory\n\n"


def assemble_claude_md(
    *,
    shared_primer: str,
    profile: str,
    memory_snapshot: str,
    runtime_identity: str = "",
    workspace_shared_status: str = "existing",
) -> str:
    """Produce the per-agent CLAUDE.md. Order: primer (platform
    conventions) → runtime identity + editable profile → standing memory.
    """
    parts: list[str] = []
    if shared_primer.strip():
        workspace_context = _workspace_shared_context(workspace_shared_status)
        if _WORKSPACE_STATUS_MARKER in shared_primer:
            shared_primer = shared_primer.replace(
                _WORKSPACE_STATUS_MARKER,
                workspace_context,
                1,
            )
        else:
            shared_primer = f"{shared_primer.rstrip()}\n\n{workspace_context}"
        parts.append(shared_primer.strip())
    role_parts = [part.strip() for part in (runtime_identity, profile) if part.strip()]
    if role_parts:
        parts.append("---\n\n# Your role\n\n" + "\n\n".join(role_parts))
    if memory_snapshot.strip():
        parts.append(MEMORY_SECTION_HEADER + memory_snapshot.strip())
    return "\n\n".join(parts) + "\n"


def _workspace_shared_context(status: str) -> str:
    if status in {"created", "existing", "mounted"}:
        return (
            "`shared/` inside it is the host-wide collaboration directory for "
            "Agents managed by the same Puffo home."
        )
    if status == "conflict":
        return (
            "`shared/` currently contains private local data and is not connected "
            "to the host-wide collaboration directory. Do not use it for "
            "cross-Agent handoffs until the operator resolves the conflict."
        )
    return (
        "The host-wide collaboration directory is unavailable in this runtime. "
        "Do not rely on `shared/` for cross-Agent handoffs."
    )


def write_claude_md(claude_dir: Path, content: str) -> Path:
    """Write ``content`` to ``<claude_dir>/CLAUDE.md`` and return the
    path. Pass the USER-level claude dir (``agents/<id>/.claude/``),
    NOT the project-level ``workspace/.claude/`` — Claude Code
    auto-discovers via ``$HOME/.claude/CLAUDE.md`` while leaving
    ``<workspace>/CLAUDE.md`` as the agent's editable layer.
    """
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "CLAUDE.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_gemini_md(gemini_dir: Path, content: str) -> Path:
    """Write ``content`` to ``<gemini_dir>/GEMINI.md``. Mirrors
    ``write_claude_md`` with the Gemini CLI filename. Pass the
    USER-level gemini dir (``agents/<id>/.gemini/``) so workspace-
    level ``GEMINI.md`` files aren't clobbered.
    """
    gemini_dir.mkdir(parents=True, exist_ok=True)
    path = gemini_dir / "GEMINI.md"
    path.write_text(content, encoding="utf-8")
    return path


def write_agents_md(codex_dir: Path, content: str) -> Path:
    """Write ``content`` to ``<codex_dir>/AGENTS.md``. codex reads
    ``$CODEX_HOME/AGENTS.md`` on ``newConversation`` as the system-
    prompt equivalent.
    """
    codex_dir.mkdir(parents=True, exist_ok=True)
    path = codex_dir / "AGENTS.md"
    path.write_text(content, encoding="utf-8")
    return path


def rebuild_agent_codex_md(
    *,
    shared_dir: Path,
    profile_path: Path,
    memory_dir: Path,
    workspace_dir: Path,
    codex_user_dir: Path,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
    workspace_shared_status: str = "existing",
) -> str:
    """Assemble + write one codex agent's AGENTS.md.

    Same content shape as ``rebuild_agent_claude_md`` (shared primer +
    agent profile + standing memory), targeting codex's
    instruction-file path. Skill bodies mirror into
    ``workspace/.agents/skills/`` where codex's project-scope discovery
    walks; the SKILL.md + frontmatter shape is identical to Claude
    Code's.
    """
    ensure_shared_primer(shared_dir)
    sync_shared_skills_codex(shared_dir, workspace_dir)
    primer = _strip_puffo_mcp_prefix_for_codex(read_shared_primer(shared_dir))
    try:
        profile_text = profile_path.read_text(encoding="utf-8")
    except OSError:
        profile_text = ""
    agents_md = assemble_claude_md(
        shared_primer=primer,
        profile=profile_text,
        runtime_identity=_runtime_identity_context(
            agent_id=agent_id,
            display_name=display_name,
            role=role,
            role_short=role_short,
            puffo_handle=puffo_handle,
        ),
        workspace_shared_status=workspace_shared_status,
        memory_snapshot=read_memory_snapshot(memory_dir),
    )
    write_agents_md(codex_user_dir, agents_md)
    return agents_md


def rebuild_agent_claude_md(
    *,
    shared_dir: Path,
    profile_path: Path,
    memory_dir: Path,
    workspace_dir: Path,
    claude_user_dir: Path,
    gemini_user_dir: Path,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    puffo_handle: str = "",
    workspace_shared_status: str = "existing",
) -> str:
    """Assemble + write one agent's managed CLAUDE.md / GEMINI.md.

    Seeds the shared primer if missing, mirrors shared skills into the
    workspace, reads the agent's ``profile.md`` and standing-memory view,
    then writes the combined prompt to the agent's USER-level ``.claude/`` /
    ``.gemini/`` dirs. The read is non-mutating and does not enforce the alpha
    briefing budget on legacy flat files.

    Shared by the worker's startup path and the ``agent reset-primer``
    CLI command so the assembly sequence lives in exactly one place.
    """
    ensure_shared_primer(shared_dir)
    sync_shared_skills(shared_dir, workspace_dir)
    primer = read_shared_primer(shared_dir)
    try:
        profile_text = profile_path.read_text(encoding="utf-8")
    except OSError:
        profile_text = ""
    claude_md = assemble_claude_md(
        shared_primer=primer,
        profile=profile_text,
        runtime_identity=_runtime_identity_context(
            agent_id=agent_id,
            display_name=display_name,
            role=role,
            role_short=role_short,
            puffo_handle=puffo_handle,
        ),
        workspace_shared_status=workspace_shared_status,
        memory_snapshot=read_memory_snapshot(memory_dir),
    )
    write_claude_md(claude_user_dir, claude_md)
    write_gemini_md(gemini_user_dir, claude_md)
    return claude_md


def rewrite_profile_name(
    profile_path: Path, old_name: str, new_name: str,
) -> int:
    """Replace whole-token occurrences of ``old_name`` with ``new_name``
    in ``profile.md`` (the prose CLAUDE.md / AGENTS.md / GEMINI.md are
    assembled from). Returns the replacement count.

    Matched only when not flanked by ASCII word characters, so
    "Bob"→"Robert" leaves "Bobcat" alone but still hits "Bob's". The
    boundary is ASCII-only (not ``\\b``, which never separates CJK
    characters), so CJK display names still match. No-op (0) on
    empty/equal names or a missing/unreferenced profile.
    """
    if not old_name or not new_name or old_name == new_name:
        return 0
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(old_name)}(?![A-Za-z0-9_])"
    )
    new_text, count = pattern.subn(new_name, text)
    if count == 0:
        return 0
    profile_path.write_text(new_text, encoding="utf-8")
    return count


# First line of the default shared primer. Used to identify
# previously-generated managed CLAUDE.md files so the worker can
# safely remove stale managed copies without touching agent-authored
# files.
_MANAGED_CLAUDE_MD_MARKER = "# Puffo.ai platform primer"


def looks_like_managed_claude_md(path: Path) -> bool:
    """True if ``path`` begins with our managed-content marker (i.e.
    was generated by ``write_claude_md``). Used to distinguish stale
    managed files we may delete from agent-authored files we must not.
    """
    if not path.is_file():
        return False
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return False
    return first_line.strip().startswith(_MANAGED_CLAUDE_MD_MARKER)
