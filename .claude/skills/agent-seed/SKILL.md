---
name: agent-seed
description: Seed a cloud agent's memory from a local agent and check it can serve — the `tools/seed_cloud_agent.py` workflow (create in the Hub → seed --to <slug> --deliver → --verify), what is carried and what is deliberately not, the "stored vs delivered" rule, and exactly which credentials a checkout does NOT give you. Use when asked to seed, import, or "migrate" an agent to the cloud, when a cloud agent answers from an empty brain, or when `--verify` reports a failure.
---

# Seeding a cloud agent from a local one

The tool is **in this repo**: `tools/seed_cloud_agent.py`. Stdlib only — a bare
`python3` runs it on a fresh checkout.

It is **not a migration**. It moves no identity: nothing is exported, rotated or
revoked, and the local agent is only ever read. Carried: the local agent's
`memory/` tree. Deliberately not carried: `keys/` (identity), `agent.yml`
(identity + a gateway credential), `workspace/`, `Library/`, `messages.db`, and
the platform-managed `briefing/profile.md` — that file names the *source* agent
and the platform regenerates it, so seeding it hands the new agent a stale
identity. The profile itself is set in the Hub's create dialog; the store owns it.

## Inputs — two handles, ask for whichever is missing

| handle | what it is | where it comes from |
|---|---|---|
| **local agent prefix** | the directory name under `~/.puffo-agent/agents/`, e.g. `optionexpe` (a prefix is enough; ambiguous → the tool refuses) | the user names it; `ls ~/.puffo-agent/agents/` lists candidates |
| **cloud slug** | the target's id, e.g. `optionexpe-7146-d57f9ef1` | the Hub profile URL `/chat/agents/<slug>/profile`, or `./aim -e staging status` in cloud-infra |

If the request names neither, **ask for both** before running anything. If it
names the local agent but not the target, look the slug up (`aim status`) and
**confirm it with the user when more than one candidate exists** — never guess a
target for a write. The cloud agent must already exist (the Hub creates it; no
CLI can).

## The workflow — one command after the Hub

```bash
# 1. create the cloud agent in the Hub, pick its LLM plan. Note its slug:
#    the URL is /chat/agents/<slug>/profile, or `./aim -e staging status` in cloud-infra.

# 2. seed AND deliver in one go (dry-run first if unsure what will be sent)
#    --profile also sets the cloud profile to the local profile.md verbatim —
#    use it unless you pasted a Soul BODY (not a whole profile.md) in the Hub.
python3 tools/seed_cloud_agent.py --from <local-agent-prefix> --dry-run
python3 tools/seed_cloud_agent.py --from <local-agent-prefix> --to <cloud-slug> --profile --deliver

# 3. check it can serve — ten checks
python3 tools/seed_cloud_agent.py --verify <cloud-slug> --expect-auth-mode subscription
```

`--from` takes a slug **prefix** and refuses if it is ambiguous. `--all` seeds
every local agent but cannot be combined with `--to` (that would merge every
brain into one agent).

## Stored is not delivered — the rule that explains every surprise

An upload puts memory in the store. A sandbox reads the store only when it
**boots**. So memory uploaded *after* the agent was created sits in the store
while the agent answers from an empty brain — `--verify` reports
`memory present: 0 file(s)`.

`--deliver` fixes that without a reboot: it asks the platform to seed the
**running** sandbox, but only if its memory tree is empty (seed-once — an agent
that has written anything is left alone, and the server says so). Rebooting is
not an alternative: a cloud agent's identity keys live only in its sandbox, so
recreating it breaks the agent in the Hub.

`--deliver --to <slug>` alone delivers memory uploaded earlier. It refuses a
paused agent — resume it first (`./aim -e staging resume <slug>` in cloud-infra).

## What a checkout does NOT give you — read this before running anything

The instructions and the tool travel with the repo. **The credentials do not,
by design.** Nothing here works against a real deployment until an operator
gives you these:

| variable | needed by | what it is | who has it |
|---|---|---|---|
| `AIM_CONTROL_URL` | upload, `--deliver` | the control API, e.g. `https://aim-staging.puffo.ai` | not secret |
| `AIM_CONTROL_TOKEN` | upload, `--deliver` | the **platform** control token — it authorises every operation on every agent | the deployment operator only |
| `E2B_API_KEY` | `--verify` | the **platform** E2B key — control over every sandbox in the account | the deployment operator only |

Both secrets are platform-wide, not per-user, so today these steps are
**operator-only**. Without them the tool fails with a clear message naming the
variable; it never partially runs. `--verify` also needs the E2B SDK:
`pip install e2b`.

In a cloud-infra checkout the control token is already resolvable — `./aim -e
staging …` reads it itself — so an operator working there exports it once:
`AIM_CONTROL_TOKEN=$(…)`; never paste it into a shell you share.

## Reading `--verify`

Each check is `ok` / `FAIL` / `????`. **`????` is not a pass** — it means the
fact could not be read. The common one: `talks to the right upstream` is
undetermined on an agent that has not spoken since it booted, because there are
no sockets to judge; it resolves on its first message.

## Known behaviours that look like bugs

- **The agent's profile mentions `/workspace/...` paths that do not exist.**
  Those are the local docker mount paths. In the cloud the runtime reads memory
  from `~/.puffo-agent/agents/<slug>/memory/` regardless; the profile text is a
  portability item, not a seeding failure.
- **Re-uploading does not change a running agent.** Seed-once. The new upload
  is stored; the agent keeps what it has.
- **`BAD_FRAME` in the logs.** Pre-existing platform noise, unrelated.

## Boundaries

Creating, pausing, resuming an agent is the Hub or cloud-infra's `aim-agent-ops`.
The store, the sweep and the deliver route are cloud-infra's `agent-seed`
skill (AIM-side internals). This skill is the user-facing workflow only.
