# Claude Runtime Recovery Investigation

Date: 2026-08-18

## Question

Does `puffo-agent` recover reliably when a Claude Code Agent cannot continue
its persisted provider session, and do PRs #253 and #255 cover every observed
way that the Agent can become permanently stuck?

## Observed Failure Families

### 1. Context admission blocks before a provider turn starts

One production Claude Agent has 79 durable pending messages. Its last
successful turn ended on 2026-08-17. Since then the daemon has repeatedly
planned work but has never started another provider turn.

Representative admission evidence:

```text
used_tokens=495678
projected_tokens=503475
context_window=1000000
outcome=degraded
```

The context controller's soft target is 50% of the provider window. Once the
projected turn crosses that target, admission requires native compaction or a
rollover. Claude reports neither capability at process open when
`auto_compact_threshold_pct` is unset. The Claude stream-json process only
advertises `/compact` in `system/init`, and current Claude Code does not emit
that frame until it receives its first user frame. Admission therefore blocks
the frame that would disclose the capability needed to unblock admission.

PR #253 makes a configured `auto_compact_threshold_tokens` available at open
time and injects the matching `--autocompact` argument. It does not help
existing/default Agent configurations where both compact threshold fields are
unset. The affected production Agent is still in that default state after the
2.0.0a17 restart.

Two other Claude Agents remained in the same degraded state for roughly 20
hours before a process restart happened to reopen them with lower reported
context use. No native compaction event was recorded, so that recovery is not
a deterministic fix.

### 2. A started provider turn fails on a crashed or invalid resumed session

PR #255 addresses a later failure boundary. A provider turn has already been
started, but the runtime either exits or reports a failed first turn because
the persisted `--resume` target is no longer usable.

Two defects are plausible and independently useful to fix:

1. `TURN_ABANDONED(retryable=True)` was converted into a non-retryable
   `RuntimeStateError`, bypassing Global Inbox's session-transfer retry path.
2. Claude can accept process startup for an invalid resume ID and report the
   failure only after the first user frame, so synchronous open fallback is
   insufficient.

PR #255 propagates retryable terminal events as `AgentAPIError` and tracks an
unconfirmed resumed runtime until one provider turn succeeds. It clears the
native session ID and retries from a fresh session when that first resumed
turn fails or the runtime exits.

## Review Concerns In PR #255

The retryable terminal propagation follows the existing Global Inbox recovery
contract and appears directionally correct.

The unconfirmed-resume classification is currently broader than its evidence:
every non-successful first turn on a resumed session is treated as proof that
the resume ID is invalid. A valid resumed session can have a first turn fail
for unrelated reasons such as rate limiting, authentication, policy rejection,
provider execution failure, cancellation, or malformed input. Resetting that
session loses provider-native history and replays only Puffo's reconstructed
current turn. Invalid-resume recovery should use a normalized provider reason
or a deliberately documented fail-open tradeoff, not `outcome != succeeded`
alone.

PR #255 also does not execute when context admission returns `degraded`,
because no provider turn or terminal runtime event exists at that point.

## Required Invariants

1. Durable pending messages are never marked processed until a correlated
   provider turn succeeds.
2. A runtime crash cannot leave a durable active turn permanently owned by a
   dead provider session.
3. A provably invalid resume target is retired, and the exact admitted message
   union is transferred once to a fresh session.
4. A transient first-turn failure must not silently discard a valid provider
   session and its history.
5. Context admission must always have a bounded forward path: compact, roll
   over, or explicitly retire to a fresh session. It cannot repeatedly return
   `degraded` without changing the state that caused degradation.
6. Repeated admission degradation must be observable as unhealthy/stuck, not
   merely as an online process with `health=unknown`.
7. Recovery remains bounded by the existing retry budget and cannot create a
   hot restart loop.

## Independent Review Questions

1. What is the narrowest reliable signal that a Claude `--resume` target is
   invalid after process startup?
2. If Claude's stream-json result does not currently expose that signal, where
   should raw provider output be normalized into a stable error code?
3. What startup capability contract avoids the pre-init compaction deadlock
   without assuming unsupported behavior from older Claude Code releases?
4. Should the default Claude runtime pass `--autocompact auto`, derive a token
   threshold from Puffo's context policy, or use a separate bootstrap path?
5. Which minimal tests distinguish invalid resume, transient first-turn
   failure, runtime exit, and pre-turn context admission deadlock?

## Evidence From Claude Code

The current CLI answers `get_context_usage` before its first user frame and
returns the provider's actual context controls:

```text
totalTokens
rawMaxTokens
autocompactSource=auto
autoCompactThreshold
isAutoCompactEnabled=true
```

This removes the bootstrap cycle without inventing a Puffo default: context
admission can query usage, learn that native compaction is available, and send
`/compact` before admitting the pending message batch.

An invalid resumed conversation also has a precise provider signal. Claude's
failed result contains `No conversation found with session ID: ...`; the
Driver can normalize that private text to `error_code=invalid_resume` without
exposing the raw diagnostic to the public event stream.

## Independent Fable Review

Claude Fable 5 reviewed the same source independently. It agreed that
retryable terminal propagation is necessary and identified four additional
state-machine gaps:

1. capabilities learned after `open` were hidden behind the immutable
   `RuntimeOpened` snapshot;
2. every first resumed failure was being classified as an invalid resume;
3. the stale native session ID was cleared after terminal persistence;
4. retry delivery inherited the generic full-payload behavior even when the
   original provider session survived.

It also noted that a resource-only reload could mark an already-confirmed
session unconfirmed again. Its suggestion to force a new default percentage
was not adopted because the provider already reports its real threshold and
the user-led 50% soft-target policy remains authoritative.

## Chosen Resolution

1. Extend provider context status with optional native autocompact metadata.
2. Let Drivers expose capabilities learned after startup; use those dynamic
   capabilities for admission and Inbox delivery decisions.
3. Preserve the existing 50% soft target. Do not add a guessed Claude CLI
   threshold.
4. Normalize only Claude's explicit missing-conversation result to
   `invalid_resume`; unrelated first-turn failures preserve the session.
5. Clear a proven-invalid session before persisting its terminal event, while
   retaining the old ID on the diagnostic event itself.
6. Remember which native session has already completed a successful turn so a
   resource reload cannot re-arm invalid-resume detection for that session.
7. Retry with a short continuation only when the native session survives;
   otherwise send the exact durable fallback to the fresh session.

The resulting recovery paths stay bounded by the existing Global Inbox retry
budget. No new retry loop or provider-specific policy threshold is introduced.
