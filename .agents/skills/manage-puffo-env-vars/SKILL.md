---
name: manage-puffo-env-vars
description: Add, rename, migrate, or review Puffo-owned environment variables and their config precedence. Use when touching `PUFFO_*` variables, raw `os.getenv` or `os.environ` reads, test/debug switches, deployment overrides, or environment-to-config migrations.
---

# Manage Puffo Environment Variables

First decide whether the setting belongs in daemon/agent config, a CLI flag, or
an environment variable. User-facing persistent behavior belongs in config;
environment variables are for deployment, expert, debug, or test overrides.

## Ownership

- Centralize Puffo-owned variables behind typed accessors. Do not add another
  scattered `os.getenv("PUFFO_...")` call.
- Read operating-system and upstream variables such as `HOME`, `PATH`,
  `CODEX_*`, provider variables, and proxy variables as external inputs rather
  than registering them as Puffo-owned settings.
- Give each Puffo variable one parser, default, owner, and precedence rule.
- In tests, restore the exact prior environment after an override.

## Naming And Migration

Use the `PUFFO_` prefix and a name that describes positive behavior. Avoid
double-negative flags. For a rename, accept the old name at one boundary,
prefer the new name when both exist, and emit a deprecation warning. Do not
silently change the default during a rename.

Trace environment values through subprocess and container inheritance. Record
whether a value is intentionally forwarded, translated into config, or kept
host-only.
