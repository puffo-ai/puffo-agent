---
name: write-puffo-python-tests
description: Add, review, consolidate, or remove Puffo Python tests with distinct regression value. Use for pytest or pytest-asyncio changes, message/runtime regressions, contract tests, concurrency tests, fixture design, test-file splits, or deciding whether a proposed test belongs in the suite.
---

# Write Puffo Python Tests

Read `tests/AGENTS.md` first. State the exact production regression each test
guards before writing it.

## Choose The Smallest Boundary

- Use a unit test for parsing, routing, state transitions, and pure policy.
- Use a contract test when a field crosses Driver, MCP, WS, HTTP, SQLite, or
  config boundaries.
- Use an integration test only when the failure requires real collaboration
  between components.
- Use `puffo-local-chat-replay` for observed end-to-end conversation behavior;
  do not encode model wording as a deterministic unit test.

Prefer an existing test module and fixture. Mock provider, network, clock, and
filesystem boundaries, not the Puffo logic under test. Use deterministic task
ordering for async and concurrency regressions.

## Regression Workflow

1. Reproduce the failure with the smallest case.
2. Observe the test fail on broken code. If behavior already passes, use a
   temporary plausible mutation to prove the assertion can fail.
3. Implement the change without weakening the assertion.
4. Run the focused test, then the full suite.
5. Remove any older case that now guards the same failure mode.

Keep test files below 2,000 lines and test functions below roughly 100 lines.
Split by behavior and ownership, not setup convenience.
