# Seed a cloud agent from a local agent — runbook

The exact commands, in order, and the two handles you need. Verified end to end
on 2026-09-12 (`optionexpe-9840-999e4666` → `optionexpe-7146-d57f9ef1`, 10/10).

## Shortcut — one command from a cloud-infra checkout

```bash
aws sso login --profile puffo-staging-admin
./aim -e staging seed desk-6332-b73d5e96 desk-iv        # <local prefix> <cloud slug or prefix>
```
`aim seed` does steps 1–5 below by itself (credentials from Secrets Manager, slug
lookup, resume, dry-run, `--profile --deliver`, `--verify`) and fetches this repo
next to cloud-infra if it is missing. Exit `2` = target already holds memory
(seed-once): delete + recreate it in the Hub. The rest of this page is the
long form — what the command does and how to run the pieces by hand.

## What you need before you start

### Two handles
| handle | example | where it comes from |
|---|---|---|
| **local agent prefix** | `optionexpe` | the directory under `~/.puffo-agent/agents/` — a unique prefix is enough (`ls ~/.puffo-agent/agents/`) |
| **cloud slug** | `optionexpe-7146-d57f9ef1` | the Hub profile URL `/chat/agents/<slug>/profile`, or `./aim -e staging status` from a cloud-infra checkout |

### Three credentials (operator-only today — a checkout never carries them)
| variable | needed by | value |
|---|---|---|
| `AIM_CONTROL_URL` | upload · `--profile` · `--deliver` | `https://aim-staging.puffo.ai` |
| `AIM_CONTROL_TOKEN` | upload · `--profile` · `--deliver` | the platform control token; a cloud-infra checkout resolves it (see step 1) |
| `E2B_API_KEY` | `--verify` | in `~/.env`; the platform E2B key |

### Two preconditions
- puffo-agent checkout on branch **`feat/seed-to-s3`** (tool flags and skill live there until merged).
- **The cloud agent already exists** — created in the Hub (name, runtime, model, LLM plan). No CLI can create a Hub-visible agent; the Hub mints identity, bridge token, group membership and the slug.

## The commands

```bash
# 0. the tool's branch
cd /Volumes/External/workspace/puffo-agent && git checkout feat/seed-to-s3 && git pull

# 1. credentials (token resolved in-process, never printed; only E2B_API_KEY from ~/.env —
#    sourcing all of ~/.env also exports the wrong AWS account's keys)
export AIM_CONTROL_URL=https://aim-staging.puffo.ai
export AIM_CONTROL_TOKEN="$(cd /Volumes/External/workspace/cloud-infra && PYTHONPATH=services/aim/src ./.venv/bin/python -c 'from puffo_aim.aim.environments import ENVIRONMENTS; print(ENVIRONMENTS["staging"].token())')"
export E2B_API_KEY="$(grep -E '^E2B_API_KEY=' ~/.env | cut -d= -f2-)"

# 2. the two handles
export LOCAL=optionexpe                       # local agent prefix
export SLUG=optionexpe-7146-d57f9ef1          # cloud slug (from the Hub URL or `aim status`)

# 3. see what will be sent — nothing is written
python3 tools/seed_cloud_agent.py --from "$LOCAL" --dry-run

# 4. profile + memory + deliver, one line
#    --profile : set the cloud profile to the local profile.md verbatim (persona identical to local)
#    --deliver : seed the RUNNING sandbox now, then restart its worker (no reboot, identity kept)
python3 tools/seed_cloud_agent.py --from "$LOCAL" --to "$SLUG" --profile --deliver

# 5. verify — ten checks (this venv has the e2b SDK)
/Volumes/External/workspace/cloud-infra/.venv/bin/python tools/seed_cloud_agent.py \
  --verify "$SLUG" --expect-auth-mode subscription --expect-template syr61j900rp69dauzqyf
```

**If step 4 says `agent is auto_paused`** — the sandbox's 30-minute lifetime expired; resume, then re-run step 4:
```bash
(cd /Volumes/External/workspace/cloud-infra && ./aim -e staging resume "$SLUG")
```

## What each step should print
| step | expected |
|---|---|
| 3 | `would upload memory for 1 agent(s)` · the file count · `profile.md is NOT uploaded — the agent store owns it` |
| 4 | `profile set from local profile.md (N bytes)` · `uploaded N memory file(s)` · `delivered N … seeded into the running sandbox; worker restarted` |
| 5 | `10 passed, 0 failed` (the upstream check reads `????` until the agent has spoken once — not a failure) |

## Confirm in chat — ask the agent itself
| ask | expect |
|---|---|
| *What are the byte count and md5 of your profile.md, next to agent.yml?* | same as `wc -c` / `md5` of the local `profile.md` — **identical** |
| *List every file under your memory directory with byte size and md5.* | every non-briefing file identical to local; `briefing/profile.md` differs (see below) |
| *In briefing/profile.md, quote the "You are" line.* | **the cloud slug**, never the local one |

`memory/briefing/profile.md` is the one file that must **not** be identical: its identity block is regenerated for the cloud agent at every worker start; everything else in it (the Soul, any text outside the managed markers) matches local.

## What is deliberately not carried
`keys/` and `agent.yml` (identity — carrying them would make this a migration, not a seed), `workspace/`, `Library/`, `messages.db`. Seeding never touches the local agent; it only reads it.

## Triggering it with the skill instead

In a Claude Code session whose working directory is **puffo-agent** on `feat/seed-to-s3`:

```
/agent-seed
seed optionexpe into optionexpe-7146-d57f9ef1 — profile, memory, deliver, then verify
```

The skill needs the **two handles**. Give both in the sentence and it runs steps 3–5. If you name only the local agent, it looks the slug up with `aim status` and **asks you to confirm when more than one candidate exists**; if you name neither, it asks for both before running anything. Credentials it cannot invent — it asks for the three variables if they are not exported.

From a **cloud-infra** session, `/agent-seed` loads the AIM-side half (slug lookup, resume, where the token comes from) and points at the puffo-agent tool for the commands.
