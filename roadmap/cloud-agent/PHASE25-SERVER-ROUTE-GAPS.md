# Phase 2.5 (server) — keyless-path route & frame gaps

This is the single spec for a future **puffo-server** run (working name:
`fleet/cloud-agent-sandbox-token-auth`). It records every place the
keyless (`transport: "bridge"`) `puffo-agent` path is already coded to
*emit* or *read* a field/name but the server does not yet *carry* it or
*authenticate* the request over the `x-sandbox-token` sandbox credential.

Until these land the agent side **degrades gracefully**: threaded replies
render top-level, names render as `@slug`. Nothing on the agent crashes —
every reader uses `.get()` with a benign fallback and every enrichment
helper returns empty/None offline. This doc is the checklist for closing
those gaps on the server; no agent change is required when they land
(the field names and query shapes below are already what the agent
emits/reads).

Scope note: the agent side of all of this is done. This run does **not**
build a token-authed REST client — per the audit below there is currently
**no** `x-sandbox-token`-authenticated REST route to call, so there is
nothing to route through. The signed `crypto/http_client` stays retired
on the bridge path; these routes are the server work that would give the
agent something to call.

---

## 1. Inbound `Message` frame — missing thread + display fields

**Frame:** `AgentServerMsg::Message` (`puffo-server` `.../bridge.rs`),
emitted to the cloud-agent WS on inbound delivery. On every branch checked
(`dev`, `cloud-agent-channel-frame`, `cloud-agent-send-threading`) it emits
only:

```
envelope_id, sender_slug, space_id, channel_id,
recipient_slug, sent_at, plaintext
```

**Add these fields to the frame:**

| Field | Agent caller (already reads it) | Effect once present |
| --- | --- | --- |
| `thread_root_id` | `_payload_from_bridge_frame` (`puffo_core_client.py`) — maps `frame.get("thread_root_id")` onto `MessagePayload.thread_root_id` | inbound replies render/store as threaded instead of top-level |
| `reply_to_id` | `_payload_from_bridge_frame` — maps `frame.get("reply_to_id")` onto `MessagePayload.reply_to_id` | reply linkage surfaces on the stored row |
| `sender_display_name` (+ optional `avatar_url`) | `_preseed_frame_display_name` → `set_profile(...)` → `_handle_plaintext_payload`'s `_fetch_display_name` cache hit | sender renders by name with **no** HTTP lookup |

Notes:

- The agent reads `thread_root_id` / `reply_to_id` **today** and runs them
  through the strict same-channel `_validate_incoming_parent_id` admit
  check before storage, so a populated frame is honored the instant the
  server adds the fields — forward-compatible, no agent change.
- `sender_display_name` is read defensively (`sender_display_name` first,
  then `display_name`); either key works. A non-empty value pre-seeds the
  profile cache so the render path fires no `/identities/profiles` GET.
- The **outbound** half of threading (`AgentClientMsg::Send` carrying
  `reply_to_id` / `thread_root_id`) is a separate server branch
  (`fleet/cloud-agent-send-threading`, `bridge.rs`). The agent already
  emits both snake_case field names on the `send` frame; the server only
  has to accept + persist them.

---

## 2. `GET /identities/profiles?slugs=<slug>` — needs an `x-sandbox-token` read variant

**Agent caller:** `_fetch_user_profile` / `_fetch_display_name`
(`puffo_core_client.py`), for `sender_display_name` on every inbound
render, plus the MCP `whoami` / profile tools.

**Today:** signed-auth only (`puffo-server` `.../lib.rs`). The sandbox token
authenticates **only** the cloud-agent control WS
(`/v2/cloud-agents/subscribe`, `v2/router.rs`), not this REST route. Over
the bridge the agent's `http` has no signing key, so the call degrades to
empty and the name renders as `@slug`.

**Add:** an `x-sandbox-token`-authenticated read variant returning
`{ profiles: [{ slug, display_name, avatar_url }] }` (same body shape the
signed route returns). Until then, the frame-carried `sender_display_name`
in §1 is the only name source on the keyless path.

---

## 3. `GET /spaces/{space_id}/channels` — needs an `x-sandbox-token` read variant

**Agent caller:** `_resolve_channel_name` (`puffo_core_client.py`) — turns a
`ch_<uuid>` into a human channel name for the `channel:` prompt line.

**Today:** signed-auth only (`membership.rs` / `space_config.rs`). Keyless →
the channel renders as its raw id.

**Add:** an `x-sandbox-token` read variant returning the space's channel
list (`{ channels: [{ id, name, ... }] }`). There is **no** frame-carried
channel-name source, so this route is the only fix.

---

## 4. `GET /spaces/{space_id}/members` — needs an `x-sandbox-token` read variant

**Agent caller:** `_get_space_members` (`puffo_core_client.py`) — scopes
`@mention` resolution + the `is_bot` flag to real space members.
Relatedly, `_resolve_space_name` needs a `GET /spaces` read variant to
turn `space_id` into a space name.

**Today:** signed-auth only (`membership.rs`). Keyless → mentions can't be
scoped to members and the space renders as its raw id.

**Add:** an `x-sandbox-token` read variant of `GET /spaces/{space_id}/members`
(`{ members: [{ slug, role }] }`) and of `GET /spaces`
(`{ spaces: [{ id, name }] }`). No frame-carried source exists for either.

---

## Summary — what closes each gap

| Gap | Fix | Home |
| --- | --- | --- |
| inbound thread ids (`thread_root_id`, `reply_to_id`) | add to `Message` frame | `bridge.rs` (`fleet/cloud-agent-sandbox-token-auth`) |
| inbound sender name (`sender_display_name`, `avatar_url`) | add to `Message` frame | `bridge.rs` |
| outbound thread ids | accept `reply_to_id`/`thread_root_id` on `Send` | `fleet/cloud-agent-send-threading` (`bridge.rs`) |
| sender display name (fallback) | token-read `/identities/profiles` | `lib.rs` |
| channel name | token-read `/spaces/{space_id}/channels` | `membership.rs` / `space_config.rs` |
| space members + names | token-read `/spaces/{space_id}/members`, `/spaces` | `membership.rs` |

`fleet/cloud-agent-sandbox-token-auth` is the likely future home branch for
the token-authed read routes; the frame additions may co-land there or with
the threading branch. All of it is server-side — the agent already emits and
reads the names above and degrades gracefully until they arrive.
