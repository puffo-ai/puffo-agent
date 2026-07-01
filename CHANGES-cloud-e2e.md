# Cloud-E2E thin-agent changes (`feat/cloud-e2e-thin`)

Branch off `origin/fleet/puffo-agent-thin-refactor`. **Never merged to puffo-agent
`main`/`dev`** — it exists only so the cloud-agent E2E (cloud-infra
`docs/integration-board.md`, T2c) can pin its E2B template build to a ref where
the thin runtime completes the messaging round-trip. The **full/local agent is
untouched** — every change here is inside `packages/puffo-agent-cloud`.

Owner: S3-messaging. Each change is listed with what + why.

---

## 1. Auto-reply a text answer back to the DM sender / channel

**Files:** `packages/puffo-agent-cloud/src/puffo_agent_cloud/runner.py`,
`tests/test_api_puffo_runner.py`.

**What:** `ApiPuffoRunner._run_turn` now returns `(reply_text, posted)` — the final
assistant text and whether a `send_message` tool_use already delivered a message
this turn. `_run_turn_for_frame` uses that: if the model answered in **text** with
**no** `send_message` tool_use, it posts that text back to where the message came
from — a DM to the inbound `sender_slug`, or the originating `space_id`/`channel_id`
for a channel message. A turn that already sent via the tool sets `posted=True` and
is **not** double-posted.

**Why:** the thin runner previously only delivered a reply when the LLM emitted a
`send_message` **tool_use**; a plain-text `end_turn` answer was computed, logged,
and dropped. In the E2E (and with the canned LLM stub, which returns text), that
meant "the agent thinks but never replies." A DM to the agent has an obvious reply
target (the sender), so auto-posting a text answer makes a simple, non-tool-using
model conversational — closing the "reply comes back" leg of the round-trip. Tool-
using models are unaffected (they set `posted` and keep full control of routing).

**Proven:** unit test `test_runner_auto_replies_text_to_dm_sender` (text answer →
one `send` to the DM sender; the existing tool-use E2E test still asserts exactly
one send, i.e. no double-post). Live: modified runtime seeded as a human, DM'd via
`bridge_client` against the standing puffo-server + canned stub → runner logged
`auto-replied to <sender>` and the sender observed the reply.

**Interaction with the deferred server gap:** for a human to *open* an operator-
bound Agent's reply, puffo-server `bridge::handle_inbound` must pass the sender's
operator attestation (it currently passes `None`). That is a **puffo-server**
follow-up (integration-board "#2", deferred). For the PoC the E2B agent is seeded
as a Human, so its reply opens with no attestation and this change is sufficient
end-to-end.
