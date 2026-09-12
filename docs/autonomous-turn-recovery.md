# Recovering an autonomous turn with an unknown outcome

An autonomous provider turn uses the configured `task_timeout_seconds` as an
idle deadline. Assistant output and tool progress renew it. If a tool or
permission request remains outstanding, expiry requests operator inspection
without killing that work. Otherwise the daemon records the uncertainty before
closing the provider. Pending Inbox rows stay attached to the original turn;
they are neither marked complete nor automatically retried.

The runtime health error includes the recovery `session_ref` and `turn_ref`.
The durable record is `<workspace>/.puffo-agent/turn_recovery.json`. Ordinary
refresh, restart, a late terminal, and new incoming messages do not clear it.
Inspect the original task and external results before choosing a retry.
Stopping the local provider does **not** undo an email, payment, or remote job.
Explicit retry does not guarantee deduplication.

## Authenticated operator controls

The existing paired, encrypted machine-control channel accepts these operations
for the affected agent. This patch adds no Agent MCP tool or new Web UI. Send
through the same authenticated operator transport as `runtime.cancel_turn`,
with a unique `command_id` and the exact references from the diagnostic:

| Operation | Parameters | Effect |
| --- | --- | --- |
| `runtime.inspect_recovery` | `session_ref`, `turn_ref` | Returns the record and the external-effects warning. |
| `runtime.stop_recovery` | `session_ref`, `turn_ref` | Stops the original provider if its owner is still present. Keeps tasks isolated. |
| `runtime.retry_recovery` | `session_ref`, `turn_ref`, `acknowledge_unknown_effects: true` | Requires confirmed stop, then requeues the exact durable turn once. |

For example, the decrypted command body for the paired control channel is:

```json
{
  "op": "runtime.retry_recovery",
  "agent_slug": "<affected-agent>",
  "params": {
    "session_ref": "<recovery-session>",
    "turn_ref": "<recovery-turn>",
    "acknowledge_unknown_effects": true
  }
}
```

This is a protocol example, not an unauthenticated HTTP endpoint or a shell
command. Pairing authentication/encryption and command identifiers remain the
responsibility of the existing operator client. The cloud runtime-command lane
also applies its existing configured-operator identity check and versioned
command envelope. No platform transport change or GUI release is included.

## Failure and restart behavior

The record is written atomically before stopping the provider. Confirmed stop
is recorded only after the Driver close completes. An incomplete or failed
close stays isolated: some Drivers make subsequent close calls no-ops, so a
second successful return is not accepted as proof. A new daemon also cannot
prove that a prior owner's unconfirmed provider stopped. These cases need
process-level investigation; the API deliberately refuses retry.

A restart with a confirmed stop keeps the provider unopened and retains the
operator controls. If the operator authorized retry and the daemon then crashed
during settlement, startup completes that authorized transition idempotently.
The resolved record remains as a duplicate-request guard, and the old provider
session is not resumed. The quarantined operation is not automatically replayed without that explicit authorization.

If persistence is corrupt or unavailable, recovery fails closed. Do not delete
the record as a workaround: doing so removes the replay protection. If rolling
back this code with an unresolved record, keep the affected agent paused; older
versions do not honor this gate.
