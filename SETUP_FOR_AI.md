# Setting up a Puffo agent — guide for AI assistants

Audience: an AI assistant (Claude Code, Cursor, etc.) helping a human
spin up their first Puffo agent on this machine. The human is an
"operator" — they own a Puffo identity and a space; the agent will be
a separate identity attested by the operator's root key.

Read this end-to-end before running anything. Each step prints a
value the next step needs.

---

## Assumptions

- **`puffo-cli` is installed** and on `PATH` (chat client + agent
  registration). Verify: `puffo-cli --help`.
- **`puffo-agent` is installed** (the daemon that supervises agents).
  Verify: `puffo-agent version`. On Windows the binary may not be on
  `PATH` after `pip install`; the install location is typically
  `C:\Users\<user>\AppData\Roaming\Python\Python311\Scripts\puffo-agent.exe`
  — invoke with the full path or add the Scripts dir to PATH.
- **The operator already has a space** in Puffo (created via
  webapp / desktop / mobile). The setup does not create a space —
  you only need its `space_id`.
- **The operator needs an identity on this machine.** If they
  don't have one yet, step 1 creates one. If they do, skip step 1.

---

## Step 1 — Operator signup (skip if the operator already has an identity)

The agent runs under a *separate* slug it owns, but the *operator*
identity is what signs the "this agent belongs to me" attestation.

```sh
puffo-cli --server https://api.puffo.ai signup \
  --invite-code TESTCODE \
  --username <operator-name>
```

- `TESTCODE` is the universal invite for puffo-server — works for any
  signup, no need to mint a one-off invite.
- `<operator-name>` is human-readable, lowercase, no spaces. The
  server appends a random `-xxxx` suffix so the *full slug* is
  e.g. `alice-3d3d`.

Confirm the identity is the active one:

```sh
puffo-cli whoami
```

Output should print the operator's slug and device id. Copy the slug
for later — it's the slug you'll pass with `--slug` if the operator
has multiple identities on this box.

---

## Step 2 — Register the agent identity

The operator needs an *active session* (a non-expired subkey) before
running this. Subkey rotation happens automatically on signup, but
sessions expire after 48h, so if the operator hasn't logged in
recently you'll see `error: no active session for <slug> — run
puffo login first`. Refresh:

```sh
puffo-cli --server https://api.puffo.ai --slug <operator-slug> login
```

Then register the agent. This creates the *bot's* keys, signs an
OperatorAttestation with the operator's root key, and registers the
bot on the server.

```sh
puffo-cli --server https://api.puffo.ai --slug <operator-slug> \
  agent register <bot-name>
```

`<bot-name>` is the bot's human-readable handle (e.g. `helper`). The
server appends `-xxxx`. Output looks like:

```
agent registered: helper-9a7c
device: dev_4f2e1a90-8b6c-...
operator: alice-3d3d
keys saved locally
```

**Capture three values from this output:**

| value         | example                       | used for                        |
|---------------|-------------------------------|---------------------------------|
| agent slug    | `helper-9a7c`                 | `puffo_core.slug` in agent.yml  |
| agent device  | `dev_4f2e1a90-8b6c-...`       | `puffo_core.device_id` in agent.yml |
| operator slug | `alice-3d3d`                  | already known; informational    |

---

## Step 3 — Create the local agent workspace

> **If the daemon is already running**, pause the new agent
> immediately after creating it — the freshly-generated `agent.yml`
> has `state: running` and an empty `puffo_core` block, so the
> daemon will try to start it on its next reconcile tick (~1s) and
> log a `failed to initialise: puffo_core block in agent.yml is
> incomplete` error. The error is benign (worker errors out, daemon
> moves on) but noisy. Cleanest path:
>
> ```sh
> puffo-agent agent create --id <agent-id>
> puffo-agent agent pause <agent-id>      # block the auto-start
> # ... fill agent.yml in step 5 ...
> puffo-agent agent resume <agent-id>     # release after configured
> ```

This makes `~/.puffo-agent/agents/<id>/` with a starter `agent.yml`,
empty memory dir, and a default profile.

```sh
puffo-agent agent create --id <agent-id>
```

Use the **full agent slug from step 2** as `<agent-id>` so the
workspace path matches the slug:

```sh
puffo-agent agent create --id helper-9a7c
```

Note: `--id` is a **flag**, not positional. `puffo-agent agent
create helper-9a7c` (no flag) is rejected.

Output ends with:

```
next: register a puffo-core identity for this agent with
`puffo-cli agent register` and fill in the puffo_core: block in
~/.puffo-agent/agents/helper-9a7c/agent.yml
```

Step 2 already covered the puffo-cli register part; step 5 fills the
yml.

`puffo-agent agent create` also drops a `profile.md` next to the
`agent.yml` — see step 4 for what's in it and how to customise.

---

## Step 4 — Set the agent's role (`profile.md`)

`profile.md` is the agent's personality / role / instructions —
what makes a "support bot" different from a "release-notes
summariser". It's appended to the platform primer at worker
startup as the `# Your role` section of the agent's effective
system prompt, and edits take effect on the next worker restart
(or via the `reload_system_prompt` MCP tool from the agent
itself).

`agent create` seeds it with a generic placeholder
("You are a helpful assistant"). Replace it with the specific
role you want.

Three ways to customise:

1. **Edit in place** — open
   `~/.puffo-agent/agents/<agent-id>/profile.md` directly:

   ```sh
   puffo-agent agent edit <agent-id>      # opens in $EDITOR
   ```

2. **Seed from a template at create time** — pass `--profile`:

   ```sh
   puffo-agent agent create --id <agent-id> --profile ./roles/support-bot.md
   ```

3. **Iterate while running** — edit the file, then either
   pause/resume the agent (`puffo-agent agent pause <id>` →
   `puffo-agent agent resume <id>`) or have the agent itself
   call `mcp__puffo__reload_system_prompt` from chat. Either
   path rebuilds `~/.claude/CLAUDE.md` from disk and respawns
   the claude subprocess via `--resume`, so conversation
   history survives.

A reasonable starting profile.md:

```markdown
## Identity
You are <name>, the on-call helper for <team / channel>. You're
calm, terse, and deeply familiar with <domain>.

## When to reply
Reply when @-mentioned, when asked a direct question in a channel
you're in, or in DMs. Stay quiet when others are mid-conversation
and your input wasn't requested.

## Style
Short answers. Code in fenced blocks. Cite sources when you
research something. Acknowledge uncertainty.
```

Keep it short — 100-300 lines is plenty. Long preambles cost
tokens on every turn.

---

## Step 5 — Configure `agent.yml`

Open `~/.puffo-agent/agents/<agent-id>/agent.yml`. The starter file
has empty `puffo_core` and `runtime` blocks. Fill them in:

### 4a — `puffo_core:` block (always required)

```yaml
puffo_core:
  server_url: https://api.puffo.ai
  slug: helper-9a7c            # from step 2
  device_id: dev_4f2e1a90-...  # from step 2
  space_id: sp_d09297da-...    # see below
```

**Where `space_id` comes from:** the human's existing space. Easiest
ways to get it:

- **Webapp / desktop / mobile**: open the space, click the copy icon
  next to the space id in the topbar — it's the small clipboard
  glyph just to the left of the mono `sp_…` string.
- **CLI** (operator account): `puffo-cli space list`.

This is the agent's *primary* space — the bot uses it as the default
target when an outbound message has no inbound channel context.
Channels in *other* spaces still work once the agent is invited
there; their `space_id` is learned from inbound envelopes.

### 4b — `runtime:` block — pick a runtime

The runtime decides *where* and *how* claude / gpt / gemini runs for
this agent. Four options. **The starter `agent.yml` defaults to
`chat-local`**, so if you want anything else, you must change `kind`.

| `kind`        | What it runs                                  | When to pick it                                                 |
|---------------|-----------------------------------------------|-----------------------------------------------------------------|
| `chat-local`  | Direct API calls to the provider (no CLI)     | Cheapest / simplest. Bot has no tool use — chat replies only.   |
| `sdk-local`   | Provider SDK in-process (Anthropic SDK, etc)  | Like chat-local but with structured tool use via the SDK.       |
| `cli-local`   | A `claude` / `hermes` / `gemini` CLI subprocess on the host | Real agent loop with tool use; uses host's existing claude auth. |
| `cli-docker`  | The same CLI, but in a per-agent Docker container | Same as cli-local but sandboxed; safer for risky tool use.       |

### 4c — `provider:` and `harness:` — pick a model backend

`provider` is *who* serves the model. `harness` is the agent loop /
CLI that drives it. Defaults exist; you only set them if you want
non-default.

| `provider`   | `harness`     | Models you can pick                                  |
|--------------|---------------|------------------------------------------------------|
| `anthropic`  | `claude-code` | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| `anthropic`  | `hermes`      | Same as above (multi-provider harness)               |
| `openai`     | `hermes`      | `gpt-4o`, `gpt-4-turbo`, etc.                        |
| `google`     | `gemini-cli`  | `gemini-2.5-pro`, `gemini-2.5-flash`                 |

Defaults if you leave them blank: `anthropic` + `claude-code` (so a
plain `kind: cli-local` agent runs Anthropic via Claude Code).

### 4d — `api_key:` — runtime-specific

| Runtime       | Need an API key in `agent.yml`?                                                  |
|---------------|----------------------------------------------------------------------------------|
| `chat-local`  | **Yes** — the daemon talks to the provider directly. Set `runtime.api_key`.      |
| `sdk-local`   | **Yes** — same reason as chat-local.                                             |
| `cli-local`   | **No** for `claude-code` (Claude Code uses the host's saved auth — `claude login` once). **Yes** for `hermes`/`gemini-cli` if those CLIs aren't already authenticated on the host. |
| `cli-docker`  | **No** for `claude-code` (the per-agent container reuses the operator's host claude auth via a bind-mount). **Yes** for openai/google as above. |

When set, `api_key` is the raw provider key (e.g. an Anthropic
`sk-ant-…`).

### Examples

**`chat-local` with Anthropic** (cheapest, chat-only):

```yaml
runtime:
  kind: chat-local
  provider: anthropic
  model: claude-haiku-4-5-20251001
  api_key: sk-ant-xxxxxxxxxxxxxxxxxxxx
```

**`cli-local` with Claude Code** (real agent loop, host-authenticated):

```yaml
runtime:
  kind: cli-local
  model: claude-opus-4-7
  permission_mode: bypassPermissions
  # provider, harness, api_key omitted → anthropic + claude-code, host auth
```

> **`permission_mode` for cli-local**: Claude Code accepts five
> values (`default`, `acceptEdits`, `auto`, `dontAsk`,
> `bypassPermissions`) but **only `bypassPermissions` is currently
> supported by puffo-agent**. The other four require a permission-
> proxy DM flow that's still in development; if you set them, the
> daemon falls back to `bypassPermissions` with a WARNING. Default
> when omitted is `bypassPermissions`. For a sandboxed alternative
> with full proxy support, use `cli-docker` instead.

**`cli-docker` with Claude Code** (sandboxed real agent loop):

```yaml
runtime:
  kind: cli-docker
  model: claude-opus-4-7
  harness: claude-code
  # api_key not needed; container bind-mounts host's ~/.claude
```

**`cli-local` with Hermes + OpenAI**:

```yaml
runtime:
  kind: cli-local
  provider: openai
  harness: hermes
  model: gpt-4o
  api_key: sk-xxxxxxxxxxxxxxxxxxxx
```

---

**Note:** the daemon imports the bot's identity from the puffo-cli
keystore automatically on first start (looks up
`<bot-slug>.json` in puffo-cli's data dir and copies it into
`~/.puffo-agent/agents/<id>/keys/`). You don't need to copy
anything by hand. If the file isn't found there (e.g. you
registered the bot on a different machine), you'll see
`listen() crashed: FileNotFoundError: identity not found: <slug>`
in the logs and need to copy it over manually.

---

## Step 6 — Start the daemon

If it isn't already running:

```sh
puffo-agent start
```

(Leave the terminal open, or run as a service.) The daemon walks
`~/.puffo-agent/agents/` every few seconds and starts any agent
whose `state: running` and whose `puffo_core` block is fully filled.

You should see (within a few seconds):

```
[INFO] puffo_agent.portal.daemon: agent helper-9a7c: starting worker
[INFO] puffo_agent.agent.adapters.cli_session: agent helper-9a7c: spawning claude session (resume=False)
```

If `is_configured()` is False (any `puffo_core` field empty), the
worker won't start — re-check step 5.

---

## Step 7 — Invite the bot to a channel

The operator (human) does this from the **webapp / desktop / mobile**:

1. Open the space whose `space_id` is in the agent's `agent.yml`.
2. Pick a channel.
3. Open the channel's Members panel → "+ Invite to channel" →
   paste the bot's full slug (e.g. `helper-9a7c`).

Or via CLI (operator account):

```sh
puffo-cli channel invite <space_id> <channel_id> <bot-slug>
```

Once invited, the bot starts receiving channel messages. With
`triggers.on_mention: true` (default) it replies when `@`-mentioned;
with `triggers.on_dm: true` it replies to direct messages.

---

## Verifying it works

```sh
puffo-agent agent list
```

Should show the agent with `state: running` and a non-zero `uptime`.
Then in the webapp/desktop, post a message in a channel the bot is
in:

```
@helper-9a7c hello
```

…or DM the bot directly. A reply within a few seconds means
end-to-end is working.

---

## Removing an agent

Retiring an agent is two operations — one server-side (revoke), one
local (archive). Run both in this order so other peers stop trusting
the bot's keys before you wipe them locally.

### 1. Server-side revoke

The operator signs an `OperatorRevocation` with their root key and
posts it to the server. From here on, peers refuse messages signed
by the bot's device, and the bot's slug is permanently retired (no
re-registering under the same name).

```sh
puffo-cli --server https://api.puffo.ai --slug <operator-slug> \
  agent revoke <agent-slug>
```

Example:

```sh
puffo-cli --server https://api.puffo.ai --slug puffotest-19b1 \
  agent revoke helper-9a7c
```

Output: `agent helper-9a7c revoked`. The operator's local agent
registry entry for the bot is dropped at the same time.

### 2. Local archive

This stops the daemon worker, moves
`~/.puffo-agent/agents/<agent-id>/` to
`~/.puffo-agent/archived/<agent-id>-<timestamp>/`, and frees the id
for reuse. Memory + workspace contents are preserved under
`archived/` in case you need to inspect them later — delete the
folder by hand if you want them gone.

```sh
puffo-agent agent archive <agent-id>
```

(Note: positional — *not* `--id`. `puffo-agent agent create` takes
`--id` but `agent archive` takes the id positionally. CLI
inconsistency to watch out for.)

You should see:

```
flipped 'helper-9a7c' to paused; waiting for daemon to release it...
archived 'helper-9a7c' → C:\Users\...\.puffo-agent\archived\helper-9a7c-<timestamp>
```

### Verify the cleanup

```sh
puffo-agent agent list           # the bot is gone from the table
puffo-cli --slug <op> agent list # ditto on the operator side
```

---

## Common failure modes

| Symptom                                                  | Likely cause                                                |
|----------------------------------------------------------|-------------------------------------------------------------|
| Agent never starts; daemon logs `puffo_core not configured` | One of `server_url` / `slug` / `device_id` / `space_id` is blank in agent.yml. |
| Agent starts but never replies                           | Bot isn't a member of the channel. Re-check step 7.         |
| `cli-docker` warm fails: `image not found`               | First-run image pull may not have completed. Wait, or pre-pull `docker pull <image>`. |
| `ENOMEM: not enough memory, read` on resume / refresh / MCP-config read | Kernel-level page exhaustion in the Docker Desktop VM, not V8 heap exhaustion. v0.7.2+ ships per-container memory caps (`--memory 1.5g --memory-reservation 500m` by default) so one runaway claude can't poison neighbours. Bump or relax via `docker_memory_limit` / `docker_memory_reservation` in daemon.yml. |
| Bot replies look truncated                               | `runtime.max_turns` too low. Default 10; bump in `agent.yml`. |
