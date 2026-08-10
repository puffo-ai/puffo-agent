# One-shot reminder contract

The Agent owns one-shot reminder intent, occurrence identity, plaintext
content, cancellation, delivery, and local scheduling in its per-Agent
`messages.db`. This contract is intentionally provider-neutral.

## Tools

- `create_reminder(content, target, intended_at)` requires exact non-empty
  content, a canonical target (`dm:<peer>`, `channel:<space>:<channel>`, or a
  channel thread target), and an RFC3339 timestamp with an explicit offset.
- `list_reminders(state="", limit=50)` returns reminders ordered by intended
  time and occurrence identity.
- `cancel_reminder(reminder_id)` is idempotent. It prevents an undelivered
  occurrence from firing and never removes a delivered local Inbox event.

Each create/cancel result is the same structured object containing immutable
`reminder_id`, `occurrence_id`, target, exact content, intended UTC time, and
the actual-fire, creation, cancellation, and delivery facts when present.

## Durable delivery

`reminder_occurrences` stores one immutable intent plus its sole occurrence.
The state transition is:

```text
scheduled -> claimed -> delivered
scheduled/claimed -> cancelled
```

Claiming records the actual fire time once. Delivery creates
`reminder-occurrence:<occurrence_id>` as a `local_runtime` pending Inbox row
and transitions the occurrence to `delivered` in the same SQLite transaction.
Duplicate ticks and restart recovery therefore find either the durable claim
or the committed terminal event; they cannot enqueue a second event. A late
occurrence remains a fact with its intended and actual times, not an instruction
to act, skip, reply, apologize, or stay silent.

The local event contains:

```json
{
  "event_type": "reminder",
  "reminder_id": "...",
  "occurrence_id": "...",
  "target": "channel:space:channel",
  "content": "exact Agent-authored content",
  "intended_at": "UTC RFC3339",
  "actual_fire_at": "UTC RFC3339"
}
```

It uses the existing local-order frontier and `GlobalInboxRuntime.notify()`
path. Consequently a target with an ordinary message and a reminder has one
content-free Inbox notice, one ordered `read_inbox` page, and one ordinary turn
path. Message records retain their own timestamps and message projection.

## Restart and ownership boundary

The scheduler reopens the same local SQLite file, delivers durable claimed
work before waiting, and waits only until the next durable deadline or a
create/cancel signal. This covers an online local Agent and a local worker that
restarts later.

## Encrypted remote sync v1

The Agent keeps one random 32-byte `MessageBackupDEK` under its private
`keys/` state boundary, outside `messages.db`. It is created atomically with
private file permissions and survives an Agent restart. This is the first
Python custody seam for that Core architecture role: it is used directly with
ChaCha20-Poly1305, is not derived into a Reminder key, and is never substituted
with `DatabaseDek`, an identity key, a session key, or a sandbox token.

For each occurrence, the Agent persists one immutable UTF-8 envelope before
the first upload. Its format is `puffo-reminder-aead-v1` and its canonical JSON
body contains only:

```json
{"algorithm":"chacha20-poly1305","ciphertext":"<base64url>","nonce":"<base64url>","version":1}
```

The ciphertext encrypts exactly canonical `{"content":...,"target":...}`
JSON using a fresh 12-byte nonce. Canonical AAD binds `owner_slug`,
`reminder_id`, `occurrence_id`, canonical UTC `intended_at`, and envelope
version. Every retry reuses the same stored envelope bytes; it never
re-encrypts an occurrence. The Server receives only base64url of that envelope
plus occurrence metadata, never target, content, a DEK, provider/model data,
or a payload-derived wake reason.

`reminder_occurrences` remains both the local source of truth and its embedded
outbox. A create is local revision 1. `claimed` is local crash recovery only
and uploads as Server `scheduled` without a revision change. A first local
`cancelled` or `delivered` transition becomes revision 2; delivery commits the
Inbox event and revision together. Each row records its last acknowledged
Server revision plus bounded retry and payload-free permanent diagnostic state.
An acknowledgement is a compare-and-set against the attempted revision, so a
late scheduled response cannot clear a newer cancellation or delivery.

Native Agents use signed:

- `PUT /v2/agent-runtime/reminder-occurrences/{occurrence_id}`
- `GET /v2/agent-runtime/reminder-occurrences?after=<occurrence_id>&limit=<n>`
- `POST /v2/agent-runtime/reminder-occurrences/{occurrence_id}/delivery-claim`

Bridge Agents use the same DTO through the existing keyless boundary:

- `PUT /v2/cloud-agents/agent-runtime/reminder-occurrences/{occurrence_id}`
- `GET /v2/cloud-agents/agent-runtime/reminder-occurrences?after=<occurrence_id>&limit=<n>`
- `POST /v2/cloud-agents/agent-runtime/reminder-occurrences/{occurrence_id}/delivery-claim`

PUT sends `revision`, `reminder_id`, RFC3339 `due_at`, `lifecycle`,
`lifecycle_at`, `payload_format`, and base64url `opaque_payload`. The Server
current-state snapshot returns scheduled rows and payload-free `cancelled` or
`delivered` terminal tombstones as `{occurrences,next_after}` ordered by
occurrence ID. On startup and reconnect, the Agent consumes every page,
strictly validates and decrypts scheduled envelopes, materializes only missing
or byte-identical scheduled rows, and applies terminal tombstones without
creating an Inbox event or turn. A snapshot never deletes a local-only row,
overwrites an immutable conflict, or regresses local `claimed`, `cancelled`, or
`delivered` state. One changed batch signals the existing scheduler so overdue
scheduled reconstruction takes the normal late Reminder Inbox path.

Server-acknowledged occurrences remain ineligible for local delivery until the
startup or reconnect snapshot completes. When one becomes due, each runtime
first durably records a random `claim_id` and submits `{revision,claim_id}` to
the delivery-claim route. The Server atomically returns `acquired`, `held`, or
`terminal`. Only `acquired` may enter the existing atomic local Inbox-event and
delivered-state transaction. Repeating the winning claim ID resumes
idempotently; another runtime's claim is held. `terminal` reconciles local
state without creating an Inbox row or Agent turn. Claim IDs are coordination
metadata only: snapshots never expose them and the Server never sees reminder
plaintext. Once a claim ID is durably recorded for the current non-terminal
revision, local cancellation fails closed regardless of whether the PUT
acknowledgement was committed; cancellation cannot erase possible Server
custody.

Successful local delivery immediately wakes the outbox so revision 2 reaches
the Server without waiting for the idle sync cadence. A held runtime retries
on a separate bounded cadence and consumes the terminal snapshot once the
winner uploads it. Only an unacknowledged row with both `payload_format` and
`opaque_payload` still `NULL` has never been prepared for remote transmission
and may fire while offline without a Server claim. Persisted envelope bytes
are the write-ahead fence: after envelope preparation, ambiguous network
outcomes, including a successful PUT followed by local acknowledgement loss,
fail closed until retry, snapshot, or delivery claim establishes custody.

Loss of both the Agent state and this `MessageBackupDEK` remains outside v1;
the Server does not decrypt reminders or retain a plaintext recovery copy.

The Server delivery claim provides cross-runtime election for acknowledged
occurrences. A claim is a renewable lease. Every delivery attempt revalidates
the persisted claim ID immediately before entering the local Inbox transaction;
retrying the same live claim renews it, another live claim is held, and an
expired claim may be acquired by a replacement runtime. The local acquired bit
is therefore crash-recovery metadata, never permanent delivery authority.

## Non-goals

This slice has no editing, rescheduling, recurrence, snooze, browser surface,
cloud sandbox lifecycle scheduling, automatic claim transfer, or
provider-specific scheduler behavior. Server-side decryption and recovery-key
upload are not part of this contract.
