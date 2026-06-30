---
name: suggest-channel
description: Suggest creating a new channel in the current space by posting an actionable suggestion card. The web client renders your message as a Suggested-channel card with a "Create channel" button that pre-fills the create-channel modal; tapping it opens the existing flow with the channel name pre-populated. Use when conversation surfaces a topic that wants its own room (e.g. "we should split the API design talk into its own channel") and you want to nudge a human to spin one up.
---

# Suggest a new channel

Use this skill when the current channel's conversation surfaces a topic that deserves its own room. Send a `/channel` block in your reply; the web client renders it as an actionable card with a **Create channel** button that opens the existing create-channel modal pre-filled with your fields.

## When to use

- A subtopic is taking over the parent channel and would benefit from a dedicated space (`#api-design` splitting out from a general `#engineering`).
- You want to recommend a specific channel name + description rather than just say "let's make a channel for this."
- A human owns the channel-create decision — this skill *suggests*, doesn't act.

## Format

Send a single message via `mcp__puffo__send_message` whose text contains exactly this block. The optional preamble above `/channel` (if any) is shown above the card as plain text.

```
<optional preamble — your reasoning, who should join, what it'll discuss>

/channel
name: <channel name without the leading #>
description: <one-line purpose, MAX ~100 chars>
message: <one-liner the suggester wants to surface alongside the card>
```

### Field rules

- **`name`** — the channel name as it'll appear in the sidebar. Lowercase ASCII letters / digits / hyphens are safest (matches the `slug` shape the server normalizes to); the modal accepts any Unicode but consistency reads better.
- **`description`** — a short blurb explaining the channel's purpose. The card renders this verbatim under the name; the parser truncates anything longer than ~100 chars and warns the human.
- **`message`** — optional one-liner shown above the card. Good place to mention who you think should join, what the first conversation should be, why now.

## Including suggested members

The current PUF-332 substrate doesn't carry a `members` field in the `/channel` block — the human accepts the suggestion, the create-channel modal opens pre-filled with the name and description, and they pick members manually from the existing picker. If you want to *recommend* specific members, list them in the preamble above the `/channel` block; the human will see your suggestion and can add them in the modal.

## Example

```
We've covered the new ingestion pipeline in #engineering for three days running. Splitting it out keeps the parent channel readable. Probably want @alice-1234, @bob-9999, @sentry-bot in there to start.

/channel
name: ingestion-pipeline
description: Design + rollout of the new ingestion pipeline. Status updates, decisions, blockers.
message: Spun out of #engineering 2026-06-30 to keep the parent thread reading-friendly.
```

The human reviewer taps **Create channel** → existing create-channel modal opens with `name=ingestion-pipeline` and the description pre-filled. They confirm and add members from the picker.

## What NOT to do

- Don't try to create the channel yourself via space-events. Suggestion → human approval → existing modal flow.
- Don't suggest a channel name that already exists in the active space; the modal will reject it as a duplicate and the human's tap is wasted.
- Don't put markdown inside the `/channel` block fields. Strict `key: value` per line.
- Don't suggest a new channel for every topic that wanders for ten minutes. Wait until the conversation is *clearly* its own thread.
