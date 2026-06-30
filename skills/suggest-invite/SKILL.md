---
name: suggest-invite
description: Suggest inviting a space member to a specific channel by posting an actionable suggestion card. The web client renders your message as a Suggested-invite card with a "Send invite" button that pre-fills the add-member modal with the suggested member pre-selected; tapping it opens the existing flow for human confirmation. Use when conversation surfaces someone whose perspective would help in a channel they aren't in yet.
---

# Suggest inviting a member to a channel

Use this skill when the conversation references someone who'd be a good fit for a channel they aren't currently in. Send an `/invite` block in your reply; the web client renders it as an actionable card with a **Send invite** button that opens the existing add-member modal with the suggested slug pre-selected.

## When to use

- You see a member's expertise (or a stakeholder's interest) come up in conversation and they aren't in the channel yet ("Alice has been working on this exact problem", "let's loop in @bob-9999").
- You want to recommend a *specific* invite rather than just say "we should bring someone in."
- A human decides whether to actually send the invite — this skill *suggests*.

## Format

Send a single message via `mcp__puffo__send_message` whose text contains exactly this block. The optional preamble above `/invite` (if any) is shown above the card as plain text.

```
<optional preamble — why this person should join, what they'd contribute>

/invite
member: <slug, e.g. alice-1234>
channel: <target channel — display name OR ch_<uuid>>
message: <one-liner shown alongside the card; optional>
```

### Field rules

- **`member`** — the **slug** of the person to invite (e.g. `alice-1234`). Slugs only — not display names. Look up the slug from a recent message author or via `mcp__puffo__get_user_info`.
- **`channel`** — either the channel display name (e.g. `marketing`) **without the leading #**, or a raw `ch_<uuid>`. If omitted, the card defaults to the *current* channel — usually wrong for `/invite`, so name the target explicitly.
- **`message`** — optional rationale for the human; renders above the card.

## Permissions

The card itself doesn't enforce channel-admin permissions — the underlying add-member modal will reject the invite at submit time if the human reviewer isn't allowed to invite. If you know the reviewer isn't an admin of the target channel, suggest someone who is in your preamble instead of relying on the button.

## Example

```
@alice-1234 has been shipping the OAuth refactor for a month — she'd catch the auth-token race we just hit.

/invite
member: alice-1234
channel: oauth-rollout
message: Alice can sanity-check our token-refresh discussion.
```

The human reviewer taps **Send invite** → add-member modal opens with `alice-1234` already in the candidate-selected list, targeting `#oauth-rollout`. They confirm and the invite fires through the existing flow.

## What NOT to do

- Don't try to send the invite yourself via space-events. Suggestion → human approval → existing modal flow.
- Don't use display names in the `member` field — slugs only.
- Don't put markdown inside the `/invite` block fields. Strict `key: value` per line.
- Don't suggest an invite for someone who's already a member of the target channel; the modal will show them as already-in and the human's tap is wasted. Spot-check with `mcp__puffo__list_channel_members` first if you're unsure.
- Don't fire multiple `/invite` cards in a row for the same person across multiple channels — pick the channel where they're most needed and let the human accept that one first.
