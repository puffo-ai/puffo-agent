# Keyless outbound dispatch (T23 phase 1)

How a **keyless cloud agent** (`puffo_core.transport == "bridge"`, no
signing key on disk) does all outbound tool work — and why native
(keystore-backed) agents are untouched.

## The transport picture

| Direction | Keyless (bridge) agent | Native agent |
|-----------|------------------------|--------------|
| Outbound reads (`list_spaces`, channels, members, profiles) | **unsigned HTTP** `GET /v2/cloud-agents/*` | signed `GET /spaces …` |
| Outbound sends (`send_message`, attachments) | **unsigned HTTP** `POST /v2/cloud-agents/messages` + `POST /v2/cloud-agents/blobs/upload` | encrypt + signed `POST /messages` / `/blobs/upload` |
| Auth on outbound | E2B **egress proxy injects `x-sandbox-token`** on outbound HTTPS — no key in the sandbox | Ed25519 subkey signature (keystore) |
| Inbound (receive push, `fetch_pending` / `ack`) | bridge **WS** (unchanged) | n/a |
| Sandbox lifecycle (`schedule_wake`, `keep_alive`, …) | bridge WS / token-authed (unchanged) | not registered |

**Key change vs. the earlier bridge draft:** outbound sends no longer go
over the bridge WS (`send_send` / `upload_blob`). They are plain unsigned
HTTP POSTs, so they work from **any process** — the daemon's in-process
ws-local dispatch *and* the cli-local subprocess MCP — because the egress
proxy, not the process, supplies auth. The bridge WS is retained for
**inbound only**.

Native agents keep the signed keystore path **byte-for-byte**. The whole
keyless behaviour is gated on a single flag, `PuffoCoreHttpClient.keyless`
(True iff `transport == "bridge"`), surfaced to the tools as
`PuffoCoreToolsConfig.keyless`.

## Where the seam lives

`puffo_core_tools.py` has one helper per wire read/write, each branching
on `cfg.keyless`:

- `_read_spaces` → `GET /v2/cloud-agents/spaces`
- `_read_space_channels` → `GET /v2/cloud-agents/spaces/{sp}/channels`
- `_read_channel_members` → `GET /v2/cloud-agents/spaces/{sp}/members`
- `_read_profiles` → `GET /v2/cloud-agents/identities/profiles?slugs=…`
- `_send_keyless` → `POST /v2/cloud-agents/messages`
- `_upload_blob_keyless` → `POST /v2/cloud-agents/blobs/upload`

The egress token injection is `PuffoCoreHttpClient._egress_headers`,
called only from `get_unsigned` / `post_unsigned` / `post_bytes_unsigned`.

## `list_channel_members` → space roster (documented caveat)

The server exposes **four** keyless read routes (`reads.rs`): `spaces`,
`spaces/{id}/channels`, `spaces/{id}/members` (the **space roster**), and
`identities/profiles`. There is **no keyless *channel*-members route**. So
under keyless, `list_channel_members` resolves channel→space (the existing
`_resolve_channel_space` cache) and reads the space roster
`GET /v2/cloud-agents/spaces/{space_id}/members`. Native keeps its
channel-scoped `/spaces/{sp}/channels/{ch}/members`. This is a deliberate,
documented behaviour difference — not a route we add here.

## GAP rows that flip to PASS

These six tools were GAP under the keyless bridge (they still hit the
signed keystore path with no keys) and now PASS over `/v2/cloud-agents/*`:

- `list_spaces`
- `list_channels_in_space`
- `list_channels_in_all_spaces`
- `list_channel_members` (→ space roster, per the caveat above)
- `get_user_info`
- `whoami` (builds identity from config + resolves `display_name` over the
  unsigned profiles route; the subkey is *managed server-side*, so it
  never loads the keystore)

And the **cli-local LLM subprocess** `send_message` / `list_spaces` now
authenticate **keyless** with no bridge and no daemon-RPC — the subprocess
is self-sufficient because `build_server(transport="bridge")` builds its
`PuffoCoreHttpClient` with `keyless=True` and the egress proxy supplies the
token.

## Test-only egress shim

Production leaves `PUFFO_LOCAL_SANDBOX_TOKEN` unset — the real E2B egress
proxy injects `x-sandbox-token`, and nothing is written to any config
file. For local end-to-end runs the shim reads that env var and adds the
header itself; `config.py` forwards the var into the MCP subprocess env
**only when it is present** in the daemon environment.

## Out of scope

Native/signed behaviour, the bridge WS inbound path, the lifecycle tools,
and any puffo-server change (the keyless routes already exist in
`reads.rs` / `send.rs`). See `FAT-E2B-INTEGRATION.md` for the surrounding
cloud drop-in work.
