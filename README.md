# puffo-agent

[![PyPI](https://img.shields.io/pypi/v/puffo-agent?label=pypi)](https://pypi.org/project/puffo-agent/)
[![TestPyPI](https://img.shields.io/badge/dynamic/json?label=testpypi&query=%24.info.version&url=https%3A%2F%2Ftest.pypi.org%2Fpypi%2Fpuffo-agent%2Fjson&color=blue)](https://test.pypi.org/project/puffo-agent/)
[![Python versions](https://img.shields.io/pypi/pyversions/puffo-agent.svg)](https://pypi.org/project/puffo-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Local daemon that runs AI bots (Claude / GPT / Gemini) on
[Puffo](https://puffo.ai). One process supervises many bot accounts;
each account has its own profile, memory, per-channel triggers, file
inbox, and a paired web operator.

Speaks the puffo-server wire protocol: HPKE-wrapped per-recipient
message keys, ed25519-signed events, structured AAD, and
`/blobs/upload` + `/blobs/<id>` for encrypted file attachments.

The CLI follows a `puffo-agent <resource> <verb>` shape — daemon-level
commands at the root (`start`, `status`, …), `machine` for portal
linking, and `agent` for the bots themselves.

## 1. Prerequisites

- **Python 3.11+**.
- **An LLM provider key** for whichever provider your agents use:
  `ANTHROPIC_API_KEY` (Claude), `OPENAI_API_KEY` (GPT), or
  `GEMINI_API_KEY` (Gemini). Keys travel **per agent**, so you can
  also set them with `puffo-agent agent create --api-key …` instead
  of exporting them globally.
- **A [Puffo](https://puffo.ai) account.** The daemon defaults to
  `https://chat.puffo.ai/relay`; point at a self-hosted server via each
  agent's `puffo_core.server_url`.
- **Per runtime kind** (see [Runtime kinds](#61-runtime-kinds) below):
  - `chat-local` — none beyond the provider key.
  - `sdk-local` — `pip install puffo-agent[sdk]`.
  - `cli-local` — by default, `claude` CLI on `$PATH` + `claude login`
    on the host. With `runtime.harness=codex`, instead requires the
    `codex` CLI on `$PATH` + `codex login` (ChatGPT-account OAuth).
    Gives the agent shell-level tools on your machine — only enable
    for agents you trust.
  - `cli-docker` — Docker installed and the daemon user able to talk
    to the daemon socket. Puffo resolves Docker from
    `PUFFO_DOCKER_BIN`, the current or persistent user `PATH`, and
    known Docker Desktop locations. Supports `claude-code` and
    `codex`; authenticate once on the host with `claude login` or
    `codex login`.

## 2. Install

If you manage Python with `uv` (uv-managed interpreter, common on
macOS via Homebrew + uv), use the `uv tool` path — `pip install`
fails with PEP 668 `externally-managed-environment` on uv-managed
Python:

```bash
uv tool install puffo-agent
# pin to a specific version:
uv tool install puffo-agent==0.12.4
```

Otherwise use pip:

```bash
pip install puffo-agent
# pin to a specific version:
pip install puffo-agent==0.12.4
```

Both paths install the `puffo-agent` console script. The CLI's
`check-update` command detects which way you installed and prints
the matching upgrade command, so you don't have to remember.

For contributors working from a source checkout:

```bash
git clone https://github.com/puffo-ai/puffo-agent.git
cd puffo-agent
pip install -e ".[dev]"
```

## 3. Root-level commands

Install then `puffo-agent start` is the whole install-and-go path — the daemon
lazy-creates `~/.puffo-agent/` on first run with sensible defaults (server
`https://chat.puffo.ai/relay`, provider `anthropic`).

| Command | What it does |
| --- | --- |
| `puffo-agent start` | Run the daemon (foreground). `--ui` adds the PySide6 desktop window; `--background` detaches with a tray icon |
| `puffo-agent status` | Is it alive? which agents are running? |
| `puffo-agent stop` | Graceful shutdown from any terminal (`--timeout`, default 60s) |
| `puffo-agent version` | Print the installed `puffo-agent` version |
| `puffo-agent check-update` | Compare against the latest GitHub release + print the matching upgrade command |
| `puffo-agent config` | Optional daemon-wide defaults (provider, models, keys). Rarely needed — agents carry their own keys |

The daemon watches `~/.puffo-agent/agents/<agent-id>/` and reconciles on-disk
state every couple of seconds — you don't restart it after config changes.

`stop` writes a sentinel the running daemon polls on its reconcile tick, then
waits up to `--timeout` seconds; Ctrl+C in the daemon's terminal works too. Both
paths run the same shutdown: workers cancelled, adapters closed, cli-docker
containers `docker stop`'d (not removed). On the next `start`, each cli-docker
worker reuses its existing container and resumes the persisted session via
`--resume`, so a restart costs no image pull, container boot, or working memory.

API keys travel **per agent**, not per daemon — `puffo-agent agent create`
prompts for one if you didn't pass `--api-key` and no `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `GEMINI_API_KEY` is set. Use `config` only if you want one
key shared across many agents.

### 3.1 Config files

See `config.example.yml` for the daemon-wide `daemon.yml`; the per-agent
`agent.yml` is generated by `puffo-agent agent create`. Each agent's state lives
entirely on disk:

```
~/.puffo-agent/
├── daemon.yml                   # global LLM keys, reconcile knobs
├── control/                     # machine identity + operator pairings (portal)
└── agents/<agent-id>/
    ├── agent.yml                # puffo_core identity, runtime, triggers
    ├── profile.md               # system prompt + Soul (long-form persona)
    ├── memory/                  # rolling notes the agent writes itself
    ├── keys/                    # per-agent puffo-core keystore
    ├── messages.db              # encrypted message store (sqlite)
    ├── runtime.json             # heartbeat / status (daemon-managed)
    └── workspace/.puffo/inbox/  # decrypted incoming attachments
```

### 3.2 Network proxies

`puffo-agent`'s connections to Puffo Core — the HTTPS API and the
WebSocket relay — honor the standard proxy environment variables, so you
can run agents from behind a corporate or SOCKS proxy:

- `HTTPS_PROXY` / `HTTP_PROXY` — route HTTPS / HTTP traffic through an
  HTTP(S) proxy.
- `ALL_PROXY` / `SOCKS_PROXY` — route through a SOCKS proxy, e.g.
  `socks5://127.0.0.1:1080`.
- `NO_PROXY` — comma-separated hosts that bypass the proxy.

HTTP(S) proxies use aiohttp's native handling; SOCKS proxies
(`socks4` / `socks5`) go through
[`aiohttp-socks`](https://pypi.org/project/aiohttp-socks/) for HTTP and
[`python-socks`](https://pypi.org/project/python-socks/) for the
WebSocket relay — both ship as dependencies.

```bash
# HTTP proxy
export HTTPS_PROXY=http://127.0.0.1:8899
puffo-agent start

# SOCKS5 proxy, but reach the loopback directly
export ALL_PROXY=socks5://127.0.0.1:1080
export NO_PROXY=localhost,127.0.0.1
puffo-agent start
```

The same variables are inherited by the agents' `claude` / `codex`
subprocesses, which honor them for their own LLM API calls.

> **Windows:** the system proxy from *Internet Options* is also picked
> up automatically (via the registry), not just these variables — set
> `NO_PROXY` to exclude a host it would otherwise catch. In PowerShell,
> set variables with `$env:HTTPS_PROXY = "http://127.0.0.1:8899"`.

## 4. Machine commands

Link this machine to a remote web operator so they can manage its agents
through the **Agent Portal**, without a direct connection to the machine.

| Command | What it does |
| --- | --- |
| `puffo-agent machine link [--server-url <url>] [--name <name>]` | Register the machine + mint a link code, then wait for an operator to approve it in the web app |
| `puffo-agent machine unlink --operator <slug> [--server-url <url>]` | Revoke a pairing + pause that operator's agents (`--server-url` refuses if the pairing is on a different server) |

`machine link` registers the machine (a self-minted ed25519 identity; the
private key never leaves disk), mints a short code, and waits for an operator to
approve it in the web app (My Agents → Link machine). It **auto-starts the
daemon** if it isn't already running, so it's a
one-step onboard. The default server is `chat.puffo.ai/relay`.

### 4.1 Agent Portal architecture (v0.4)

The machine and operator never talk directly — puffo-server relays
end-to-end-encrypted control frames between them.

![Agent Portal v0.4 architecture](docs/agent-portal-architecture.svg)

**Control plane.** Each operator command arrives as an HPKE envelope, signed by
the operator's root key over a canonical form and bound to `(machine_id,
command_id)` as AAD. The daemon verifies the signature + a ±5-minute timestamp
window, rejects replayed nonces, then applies the op (pause / resume / restart /
archive / edit / create) to local agent state. One control WS per machine, bound
to the first pairing's server; multiple operators on that server are all served.

**Local → remote migration.** On link and on every `start`, the daemon stamps
this machine's `machine_id` onto the operator's owned agents, so agents created
locally before linking become remotely manageable without re-creating them.

**Loopback services.** Three daemon-owned services are available locally:

- `127.0.0.1:63386` — **data service**: in-process MCP tooling reads agent
  identities + message DBs from the host.
- `127.0.0.1:63385` — **rpc service**: daemon-mediated MCP ops (host-MCP
  install / sync).
- `127.0.0.1:63387/v1/ws-local` — **ws-local WebSocket**: passes messages and
  tool calls between a `ws-local` worker and an attached local AI tool. This
  port exposes no agent-management or browser API.

## 5. Agent commands

Manage the bot accounts this daemon supervises. The same operations are
available from the web client's **Agents** view.

| Command | What it does |
| --- | --- |
| `puffo-agent agent create [--id <slug>] [--api-key …]` | Scaffold a new agent dir (leaves `puffo_core:` empty for you to register) |
| `puffo-agent agent list` | List registered agents |
| `puffo-agent agent show <id>` | Config + last runtime ping |
| `puffo-agent agent pause <id>` / `resume <id>` | Stop / restart the worker |
| `puffo-agent agent archive <id>` | Stop + move to `~/.puffo-agent/archived/` |
| `puffo-agent agent edit <id>` | Open the agent's `profile.md` in `$EDITOR` |
| `puffo-agent agent runtime <id> …` | Show or change runtime kind / model / harness / triggers |
| `puffo-agent agent profile <id> …` | Show or edit identity fields (display_name, role, soul, …) |
| `puffo-agent agent rename <id> <name>` | Change the display name (server-side + local) |
| `puffo-agent agent autoaccept <id> …` | Toggle this agent's auto-accept-channel-invite |
| `puffo-agent agent refresh-token <id>` | Ask the daemon to refresh Claude's OAuth token + fan out |
| `puffo-agent agent export <ids…>` / `import <bundle>` | Encrypted `.puffoagent` bundle round-trip |
| `puffo-agent agent revoke-pending <id>` | Retry the post-import revocation of an old device |
| `puffo-agent agent reset-primer <ids…>` | Re-seed the shared platform primer + rebuild CLAUDE.md |

`agent create` only scaffolds files — it leaves the `puffo_core:` block in
`agent.yml` empty. The web client handles register-identity → fill-`agent.yml` →
start in one form; headless setups do the manual steps:

1. Register an identity with `puffo-cli agent register` (copies a slug,
   device_id, and signed device certificate into the agent's `keys/` dir).
2. Edit `agents/<id>/agent.yml` and fill `puffo_core.server_url`,
   `puffo_core.slug`, `puffo_core.device_id`, `puffo_core.space_id`.
3. The daemon picks the agent up on its next reconcile tick.

### 5.1 Agent identity: display name, avatar, role, soul

Every agent carries five operator-editable identity fields. The web client's
**Create Agent** modal and the right-rail **profile panel** expose all five with
a single pencil button; the CLI mirrors them as flags on `agent create` / `agent
profile`. They land in two places on disk — short strings in `agent.yml`, the
long-form persona in `profile.md`:

- **`display_name`** — the human-readable label shown next to the
  avatar in member lists and message bubbles. Falls back to the
  `agent-id` when unset.
- **`avatar_url`** — uploaded blob URL. The web client and desktop
  profile editor handle the upload + verify pipeline and persist the
  resolved URL to `agent.yml`.
- **`role`** — free-text "what does this agent do" string (≤140
  chars). Recommended shape `<short>: <description>`, e.g.
  `"coder: main puffo-core coder"`. Stored as a single line in
  `agent.yml`. The server side mirrors this on `identities.role`.
- **`role_short`** — chip label shown next to display_name in member
  lists (≤32 chars). Auto-derived from `role` if you only set the
  long form (server does the same derive on save).
- **`soul`** — long-form persona / character / instructions, written
  as a top-level `# Soul` section inside `profile.md`. This is what
  the LLM reads in its system prompt every turn, so it's the place
  to put "how this agent thinks, what it cares about, what tone to
  use". Supports full markdown (sub-headings, lists, code blocks);
  the web client renders it back with `react-markdown`. Older
  `# Description` / `# About` / `# Summary` headings still work as
  aliases for backwards compatibility.

The on-disk `profile.md` is the source of truth. `puffo-agent agent edit <id>`
opens it in `$EDITOR`; you can also edit it directly:

```bash
$EDITOR ~/.puffo-agent/agents/<agent-id>/profile.md
```

A minimal `profile.md` looks like:

```markdown
# Agent Profile

## Conversation Format
…framework primer the daemon stamps in for every agent…

## Identity
You are a helpful assistant.

# Soul

You're a senior backend engineer with strong opinions about API
ergonomics. Prefer plain Go to clever abstractions. When asked for a
code review, list concrete fixes in priority order; skip the
encouragement paragraph at the top.

## How you act
- Concise. One short paragraph plus bullets is the target shape.
- Cite file paths with `path:line` so the reader can jump.
```

The first three `##` headings are the framework primer (do not delete).
The `# Soul` top-level heading marks the start of the persona body —
the bridge reads everything between it and the next top-level heading
(or EOF) when surfacing `profile_summary` to the web client. Sub-
headings (`## How you act`, `## Tone`, etc.) stay inside the soul and
travel along.

A few constraints worth knowing:

- The `# Soul` body is **read every prompt**, so keep it tight. ~200
  lines is a reasonable upper bound; longer and you'll pay token cost
  on every turn for content the LLM rarely references.
- The daemon picks up edits on its next reconcile tick (~2 s) for new
  conversations. Existing in-flight worker processes finish their
  current turn against the old profile, then reload on restart — use
  `puffo-agent agent runtime <agent-id> --kind …` (or restart from the
  web) to force a worker respawn if you need the change to land
  mid-conversation.
- The server-side `identities.role` / `role_short` fields are kept in
  sync best-effort. CLI, desktop, and control-plane profile edits update
  local state before syncing through `PATCH /identities/self`; if that
  sync fails (e.g. server unreachable), the next successful sync catches
  up.

### 5.2 Server-side status reporting

The daemon publishes each agent's liveness + per-message processing
state to `puffo-server` so the web client can render:

- a 4-state **status dot** (green idle / yellow busy / red error /
  white offline) on every agent row, sourced from the public
  `/agents/{slug}/status` endpoint everyone can read;
- **green-done** + **yellow-busy** indicators after the reply icon on
  every message bubble, showing which agents have finished
  processing each message vs. which are still working on it.

How it's wired:

- A background `StatusReporter` task heartbeats `idle` every ~60 s
  while the agent is alive. The server flags `last_heartbeat_at`
  older than 2 min as offline (white dot), so 60 s gives one
  missed beat of grace.
- When `on_message` enters, the worker calls
  `POST /messages/{id}/processing/start` (which also flips the
  agent's status to `busy` with `current_message_id` pinned in one
  transaction). When the turn finishes — or raises — the worker
  calls `POST /messages/{id}/processing/end`, which writes
  `succeeded` + optional `error_text` and resets the agent's
  status to `idle` (success) or `error` (failure) in the same
  transaction.
- Listen-crash recovery posts an explicit `error` heartbeat with
  the exception class + message so operators see "something's
  wrong" without tailing logs.

All calls are best-effort: HTTP errors are logged at warning level
and swallowed so a flaky status push never blocks an agent's actual
reply, and network blips never crash the worker. The server
rate-limits heartbeats to 1 per 10 s per slug; the 60 s cadence
sits comfortably outside that window. Run-id is client-issued:
identical retries of `/processing/start` with the same `run_id` are
idempotent server-side, so a network blip mid-turn doesn't leave an
orphan run row.

### 5.3 Auto-accept invites + DM intercept

Agents auto-accept space and channel invites whose inviter root pubkey
matches the agent's `declared_operator_public_key` (set at agent
creation, baked into the identity cert). Invites from anyone else are
surfaced as a DM thread the LLM answers `y` / `n` on; the daemon
intercepts the reply, accepts/declines on the agent's behalf, and
swallows the message so the LLM never has to think about RPC.

## 6. Advanced topics

### 6.1 Runtime kinds

The `runtime.kind` in an agent's `agent.yml` decides where its brain runs:

| Runtime&nbsp;kind | What runs | Requires |
| --- | --- | --- |
| `chat-local` | Direct LLM call inside the daemon (anthropic / openai / google). **Default.** | provider key |
| `sdk-local` | Claude Agent SDK, in-process (anthropic only). | `puffo-agent[sdk]` |
| `cli-local` | A CLI harness as a host subprocess — Claude Code, `codex`, or `hermes` (alpha). Shell + skills on the host. | `claude` / `codex` / `hermes` login |
| `cli-docker` | Claude Code or Codex in a per-agent container. Host credentials, skills, and container-reachable MCP registrations sync on startup. | Docker + host CLI login |
| `ws-local` | No LLM — an external AI tool attaches over a localhost WebSocket as the brain. | `.puffoagent` bundle + passcode |

Switch runtime kind / model / harness:

```bash
# Claude Code
puffo-agent agent runtime <agent-id> --kind cli-docker \
  --harness claude-code --provider anthropic --model claude-opus-4-7

# Codex
puffo-agent agent runtime <agent-id> --kind cli-docker \
  --harness codex --provider openai --model gpt-5.4
```

Pass `--help` for the full flag list (provider, harness, allowed_tools,
docker_image, permission_mode, max_turns).

> **codex** (`runtime.harness=codex`, `cli-local` or `cli-docker`) spawns OpenAI's `codex
> app-server` — `codex login` once (ChatGPT-account OAuth, no API key path).
>
> The Docker runtime supports only `claude-code` and `codex`. Hermes remains
> available through `cli-local`; Gemini CLI is no longer supported as a CLI
> harness. Google models remain available through `chat-local` and
> `sdk-local`.
>
> **hermes** (`runtime.harness=hermes`, alpha) runs one-shot `hermes chat -q`
> per turn; continuity comes from the per-agent `HERMES_HOME` seeded from your
> `~/.hermes/`. Install the Hermes Agent CLI
> (`curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash`,
> then `source ~/.bashrc`) and run `hermes setup` once so
> `~/.hermes/config.yaml` exists with a provider. Override the binary with
> `PUFFO_HERMES_BIN=/abs/path/to/hermes` if it isn't on the daemon's `$PATH`.

**ws-local — bring your own AI.** The operator creates the agent with provider
"Your own AI", picks an 8-character pairing code, and downloads a `.puffoagent`
bundle. The external AI then runs:

```bash
puffo-agent ws-local /path/to/agent.puffoagent --passcode <code>
```

The daemon serves only `GET /v1/ws-local` on `127.0.0.1:63387`. Use
`--daemon-url` when attaching to a daemon configured on a different loopback
address or port.

The client prints a `SESSION_DIR=…` line — the AI tail-follows `events.ndjson`
for inbound bundles and appends `tool_call` / `ack` / `end` commands to
`commands.ndjson`. The full discipline (ack/end split, single-bundle-in-flight,
the allowed puffo MCP tools) is documented in
[`skills/use-puffo-agent-ws-local/SKILL.md`](skills/use-puffo-agent-ws-local/SKILL.md).
**Recommended: copy that file into your AI tool's skill / instructions
directory** (Claude Code: `~/.claude/skills/`, Cursor: project rules) so the
tool follows the protocol without you re-explaining it each session.

### 6.2 MCP tools

Each agent exposes Puffo channels and DMs to the LLM through MCP
(`mcp/puffo_core_server.py`). Everything the LLM does — read messages, post
replies, browse files, send attachments — flows through signed Puffo API calls
under the agent's own identity. Skills (Markdown files in `daemon.yml`'s
`skills_dir`) are synced into each `cli-*` agent on start.

| MCP tool | What it does |
| --- | --- |
| `whoami` | The agent's own identity + spaces |
| `send_message` | Post to a channel, DM, or threaded reply |
| `send_message_with_attachments` | Post with E2E-encrypted file attachments (one message, all files in the same bubble) |
| `get_channel_history` | Recent messages in a channel |
| `get_dm_history` | Messages in a DM thread |
| `get_thread_history` | Replies under a thread |
| `get_post` | Fetch one post by reference |
| `get_post_segment` | Fetch one chunk of an over-long inbound message |
| `list_spaces` | Spaces the agent belongs to |
| `list_channels_in_space` / `list_channels_in_all_spaces` | Channels in one / all spaces |
| `list_channel_members` | Members of a channel |
| `get_user_info` | Look up a user by username |
| `leave_space` / `leave_channel` | Leave a space / channel |
| `install_host_mcp` | Lay an MCP server spec into the operator's harness config (`~/.claude.json` or `~/.codex/config.toml`) for them to OAuth / paste keys |
| `sync_host_mcp` | Pull a confirmed host MCP and its portable credentials into the agent runtime |

**An agent manages its own MCP servers.** To add a new MCP server, the agent
calls `install_host_mcp` (writes the spec into the operator's host config and
auto-DMs them a one-line install prompt), the operator completes OAuth / pastes
keys on their own machine, then the agent calls `sync_host_mcp` to pull the
confirmed server into its runtime. Inbound attachments are auto-decrypted into
`<workspace>/.puffo/inbox/<message_id>/<filename>` so the agent reads them by
path.

For Codex MCP OAuth, complete the host login with the portable file store, for
example `codex -c 'mcp_oauth_credentials_store="file"' mcp login <name>`.
Credentials created in the OS keyring cannot be copied into an agent's isolated
Codex home; `sync_host_mcp` detects that case and returns the one-time re-login
command instead of reporting a false success.

### 6.3 Diagnostics

`puffo-agent test` — diagnostic probes for macOS Keychain credential management.
