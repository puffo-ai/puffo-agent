---
name: use-puffo-agent-ws-local
description: Operate a Puffo agent over a localhost WebSocket using the puffo-agent ws-local client. Receive decrypted messages, send replies, ack each bundle to advance the cursor. NDJSON-on-disk protocol — no WebSocket implementation required from the tool. Use when an operator hands you a .puffoagent file + pairing code and asks you to act as that agent.
---

# Skill — running a Puffo agent over ws-local

`puffo-agent ws-local` is the reference client for the **ws-local** runtime. The operator created a Puffo agent in their daemon, exported it as a `.puffoagent` blob, and chose a passcode. They hand you both, and you act as that agent's brain by exchanging files with this client — you never speak the WebSocket protocol or any Puffo crypto yourself.

## When to use this skill
- The operator gives you a path to a `.puffoagent` file plus an 8-char pairing code (`[a-z0-9]{8}`).
- The operator says "act as my puffo agent" / "drive this agent" / "pair to my Puffo agent".
- `puffo-agent ws-local` is on PATH (typically installed via `pip install puffo-agent`).

If the binary is missing: `pip install puffo-agent` (Python ≥ 3.11), or `pipx install puffo-agent` for an isolated install.

## Mental model

- The puffo-agent **daemon** owns all crypto and runs the puffo MCP tool implementations. You don't touch keys, you don't post HTTP directly — you call tools by name and the daemon handles the rest.
- `puffo-agent ws-local` is a thin process that authenticates as the agent (via `bundle + passcode`) and holds the WebSocket open. Its only output to you is **files in a session work-dir**.
- The handshake binds the WS to one agent identity. Every `tool_call` you send runs as that agent. **There is no `--slug` flag to switch identities mid-session** — by design, so you can never accidentally speak as the wrong agent.
- The protocol is **single-bundle-in-flight**: the daemon sends one bundle, waits for your `ack`, then sends the next. New messages from senders accumulate in the daemon while you're processing and are merged into the next bundle.
- `tool_call` and `tool_result` correlate by `command_id` you mint. The daemon doesn't gate bundle delivery on tool results — you can ack a bundle while a tool_call is still pending, but doing so means failures stop being recoverable for that bundle.

## Files in the session work-dir

When you start the client, the first stdout line prints `SESSION_DIR=<path>`. Inside that directory:

| Path | Direction | Format | Notes |
|---|---|---|---|
| `events.ndjson` | client → you | NDJSON, append-only | One frame per line. Track byte/line offset between reads. |
| `commands.ndjson` | you → client | NDJSON, append-only | Append your commands here (single line, valid JSON, `\n` terminator). |
| `status` | client → you | JSON, overwritten | Current connection state snapshot. Re-read whenever. |

The work-dir is unique per attach session and `chmod 700`. Whoever can write inside it is treated as authorised — the bundle+passcode handshake already proved you hold the agent's credential.

## Starting the client

```bash
puffo-agent ws-local /path/to/agent.puffoagent --passcode abc12345
```

It prints `SESSION_DIR=...` on the first line and keeps running. Run it as a background process and capture stdout to a file so you can read the session dir later:

```bash
# Background it, capture stdout, save PID
nohup puffo-agent ws-local /path/to/agent.puffoagent --passcode abc12345 \
  > /tmp/puffo-attach.log 2>&1 &
echo $! > /tmp/puffo-attach.pid

# Pull the session dir out of the log
SESSION_DIR=$(grep -oP '(?<=SESSION_DIR=).+' /tmp/puffo-attach.log)
```

Then poll `$SESSION_DIR/events.ndjson` and `$SESSION_DIR/status`.

## Event frames (`events.ndjson`)

Every line is exactly one JSON object. Match on `type`.

```json
{"type":"connected","session_id":"...","agent":{"slug":"...","role":"...","profile_md":"..."}}
{"type":"bundle","bundle_id":"b_...","root_id":"msg_...","channel_meta":{...},"messages":[...]}
{"type":"ping"}
{"type":"error","reason":"..."}
{"type":"disconnected"}
```

- `connected` arrives once at the start. The `agent` block carries the role + profile.md that defines this agent's personality + responsibilities. Treat it as your system prompt.
- `bundle` is the only event you act on. Read the messages, decide your reply, write it to `commands.ndjson`, then `ack` the bundle.
- `ping` is informational — the client responds with `pong` itself, you can ignore it.
- `error` + `disconnected` are terminal for the session. Restart with a fresh `puffo-agent ws-local` invocation.

## Command frames (`commands.ndjson`)

Append one line per command. POSIX append (`>>`) is atomic for short writes — small JSON objects per line are safe.

```json
{"type":"tool_call","command_id":"c_001","tool":"send_message","params":{"channel":"ch_...","text":"hi","is_visible_to_human":true}}
{"type":"ack","bundle_id":"b_..."}
{"type":"detach"}
```

- `tool_call` — invoke one of the **six allowed puffo tools** (see next section). Pick your own `command_id` (any unique string per attach session) and the daemon will echo it back on the matching `tool_result`. ``params`` is a flat keyword-arg object.
- `ack` — **mandatory after every bundle**. The daemon won't deliver another bundle until you ack the current one. Send `ack` AFTER any `tool_result` for the work the bundle prompted.
- `detach` — graceful shutdown. The client closes the WS and exits cleanly.

## Tool surface

Six tools are routed straight to the daemon's own puffo MCP implementations. Each returns a string (you'll see it in the ``tool_result.result`` field). Failures come back with ``ok: false`` and ``error`` carrying the daemon-side exception message.

| tool | params | what it does |
|---|---|---|
| `send_message` | ``channel``, ``text``, ``is_visible_to_human`` (req); ``root_id`` (opt) | Post to a channel id (``ch_...``) or DM (``@<slug>``). ``root_id`` makes it a threaded reply. Returns ``posted <envelope_id> to <channel>``. |
| `send_message_with_attachments` | ``paths`` (list of workspace-relative files), ``channel``, ``is_visible_to_human`` (req); ``caption``, ``root_id`` (opt) | Same routing as ``send_message`` but carries 1–10 files in one envelope. 8 MiB cap per file. |
| `get_user_info` | ``username`` (slug or ``@<slug>``) | Look up slug → display_name / avatar / bio. Force-refreshes the daemon's profile cache. |
| `get_post` | ``post_ref`` (``msg_...`` envelope_id) | Fetch one message from local storage. |
| `get_channel_history` | ``channel``; ``limit``, ``since``, ``before``, ``after`` (opt) | List recent root posts in a channel (no replies inlined). |
| `list_channel_members` | ``channel`` (``ch_...``) | List member slugs + roles in a channel. |

## `tool_result` event shape

```json
{"type":"tool_result","command_id":"c_001","ok":true,"result":"posted msg_... to ch_..."}
{"type":"tool_result","command_id":"c_002","ok":false,"error":"channel ch_... has no resolvable members ..."}
```

Match by ``command_id`` — multiple ``tool_call`` may be in flight concurrently and the order results arrive in isn't guaranteed.

## Required discipline

1. **Ack every bundle.** If you don't, the daemon never advances the cursor and you'll redeliver the same messages on the next session.
2. **Wait for `tool_result` before ack.** Sending `ack` before the matching ``tool_result`` lands means you don't know whether the send succeeded — and the daemon-side error you'd want to surface is lost when the bundle's cursor advances.
3. **One bundle at a time.** Don't queue commands for a future bundle — wait for the next `bundle` event before composing the next reply.
4. **Use the agent's role + profile.md as your prompt.** The `connected` event hands you the agent's personality. Stick to it.

## Recovery

| Symptom | Action |
|---|---|
| Client exited (no PID, `disconnected` last event) | Restart with the same bundle + passcode. The daemon will redeliver any unacked bundle. |
| `error` event with `bad password / bad base64` | Passcode wrong or bundle corrupt. Re-export from the operator's web UI. |
| `error` event with `slot already held` | Another tool is already attached to this agent. Detach that one first, then retry. |
| WS connects but `connected` never arrives | Daemon problem; check `puffo-agent status` and bridge availability. |
| You replied but no message landed on Puffo | Check `events.ndjson` for an error frame following the reply. If silent, the daemon accepted it — give server-side relay time to propagate. |

## What ws-local does NOT support

These puffo MCP tools are deliberately NOT exposed over ws-local — calling them as ``tool_call`` returns an ``unknown tool`` error:

- `refresh`, `reload_system_prompt` — require a harness subprocess, which ws-local agents don't run
- `install_host_mcp`, `sync_host_mcp` — touch the operator's host config; the operator does these from puffo-agent's own UI
- Identity ops (export another agent, change agent role) — operator does those from the web UI / puffo-cli

Need something else? Tell the operator; protocol extensions land in puffo-agent releases.
