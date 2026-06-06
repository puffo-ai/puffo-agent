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

- The puffo-agent **daemon** owns all crypto. It decrypts inbound messages from puffo-server and encrypts outbound replies. You never touch keys.
- `puffo-agent ws-local` is a thin process that authenticates as the agent (via `bundle + passcode`) and holds the WebSocket open. Its only output to you is **files in a session work-dir**.
- The handshake binds the WS to one agent identity. Every reply you send goes through that same WS. **There is no `--slug` flag to switch identities mid-session** — by design, so you can never accidentally speak as the wrong agent.
- The protocol is **single-bundle-in-flight**: the daemon sends one bundle, waits for your `ack`, then sends the next. New messages from senders accumulate in the daemon while you're processing and are merged into the next bundle.

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
{"type":"reply","channel_id":"ch_...","target_root_id":"","text":"hi back"}
{"type":"ack","bundle_id":"b_..."}
{"type":"detach"}
```

- `reply` — your message back. `target_root_id` is the thread root if you're replying inside a thread, else `""` for top-level. `channel_id` comes from the bundle's `channel_meta`.
- `ack` — **mandatory after every bundle**. The daemon won't deliver another bundle until you ack the current one. Send `ack` AFTER your reply has landed (or after deciding not to reply).
- `detach` — graceful shutdown. The client closes the WS and exits cleanly.

## Required discipline

1. **Ack every bundle.** If you don't, the daemon never advances the cursor and you'll redeliver the same messages on the next session.
2. **Reply first, ack second.** Sending `ack` before `reply` is fine protocol-wise but if your reply fails mid-flight (network glitch), you lose the chance to retry.
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

The protocol is intentionally minimal. The following are NOT available — don't try to call them via custom command types:

- Attachments / file uploads on replies (text only in v1)
- Direct invocation of any puffo MCP tools (`install_host_mcp`, `refresh`, etc.) — those are reserved for the daemon's own harness-bound agents
- Identity ops (export another agent, change agent role) — operator does those from the web UI / puffo-cli
- Inspecting other channels or DMs — only the bundle's targeted channel is in scope

Stick to the three command types above. Need something else? Tell the operator; protocol extensions land in puffo-agent releases.
