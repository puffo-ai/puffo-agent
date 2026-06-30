---
name: suggest-agent
description: Suggest a new Puffo agent to your operator by posting an actionable suggestion card to the current channel. The web client renders your message as a Suggested-agent card with an "Add as my agent" button that pre-fills the create-agent modal; tapping it opens the existing flow with your fields already populated. Use when the conversation surfaces a need that another agent could handle (e.g. "we need someone to triage support tickets", "a reviewer for our docs PRs would help"). Do NOT use to create an agent yourself — this is a *suggestion* for a human to accept.
---

# Suggest a new Puffo agent

You want your operator (or another channel member) to consider creating a new agent. Don't try to provision it yourself — instead, send a message in the canonical `/agent` block shape and the puffo web client renders it as an actionable card with an **Add as my agent** button.

## When to use

- A conversation surfaces a recurring task that doesn't have a dedicated agent ("we should have someone watching the Sentry stream", "a release-notes drafter would unblock the PM").
- You want to recommend a specific agent shape (name + role + description) rather than just hand-waving "you should add an agent."
- A human is the right approver — this skill is for *suggesting*, not for taking action.

## Format

Send a single message via `mcp__puffo__send_message` whose text contains exactly this block. The optional preamble above `/agent` (if any) is shown above the card as plain text.

```
<optional preamble — your reasoning, context, prompt for the human>

/agent
name: <display name>
role: <short role label, e.g. "QA reviewer" or "release coordinator">
description: <plain-text purpose, MAX 108 BYTES>
message: <one-liner the agent should kick off with after it joins>
```

### Field rules

- **`name`** — what the operator should see in the agent picker (e.g. `Scout`, `Eli the Editor`). Keep it short; the card truncates to one bubble width.
- **`role`** — a short label that renders as a pill chip in the card. Two or three words max ("API reviewer", "support triage").
- **`description`** — **must be ≤ 108 bytes** (UTF-8). The card renders this verbatim under the role chip. The web parser truncates anything longer to ~100 characters and warns the operator, so count your bytes before sending. If you need more rationale, put it in the preamble above `/agent`.
- **`message`** — a one-line greeting / first prompt the agent will use after the human accepts. Optional but improves the suggestion.

### Byte counting tip

ASCII chars are 1 byte. Common non-ASCII glyphs (中文, emoji) are 3–4 bytes — count them carefully. Plain English at ~108 bytes is roughly one sentence.

## Example

```
We've been triaging Sentry alerts manually in #ops for two weeks; a dedicated agent would close the loop faster.

/agent
name: Sentry Triage
role: Incident watcher
description: Watches Sentry's high-severity stream and pings the on-call when a new error class appears.
message: Hi! I'll watch Sentry and surface unknown error classes. Acking the first one now.
```

Posting that message in `#ops` will render an actionable card with **Add as my agent**; the human reviewer can tap it to open the create-agent modal pre-filled with your fields.

## What NOT to do

- Don't omit any of `name` / `role` / `description` — the card renders with placeholder text and looks broken.
- Don't try to create the agent yourself. Suggestion → human approval → existing flow takes over.
- Don't send the same suggestion twice in quick succession — the web renders both, which looks like spam. Edit the original if you want to refine your pitch.
- Don't put markdown inside the `/agent` block fields. The parser is strict `key: value` per line.
