---
name: refactor-puffo-python-components
description: Refactor oversized or overloaded Puffo Python files, functions, and classes into cohesive components without behavior drift. Use for files over 2,000 lines, functions over roughly 100 lines, god objects, module extraction, symbol moves, class splits, or review of a structural refactor.
---

# Refactor Puffo Python Components

Read `AGENTS.md` and the tests covering the target before editing.

## Establish The Boundary

1. List the target's public imports, constructors, callbacks, side effects,
   logs, exceptions, and state writes.
2. Name the current state owner. Preserve ownership during a mechanical split.
3. Choose one cohesive responsibility to extract. Do not split by line range.
4. Classify every planned edit as prepare, move, or post-move cleanup.

Treat `Worker` and `GlobalInboxRuntime` as composition roots. They may construct,
wire, delegate, and order collaborators. Parsing, transformation, persistence,
and policy belong in leaf modules. Keep `PuffoCoreMessageClient` as a compatible
facade while moving its transport, envelope, membership, attachment, and send
responsibilities behind it.

## Perform A Mechanical Batch

1. Prepare imports or compatibility aliases while behavior remains unchanged.
2. Move one responsibility with signatures and statement order preserved.
3. Keep logs, exception types, retries, cancellation, and callback timing
   unchanged.
4. Run focused tests immediately after the move.
5. Do naming or API cleanup only after the move is green, in a separate diff.

Do not add a test-only accessor or pass a large runtime object into the new
module. Pass narrow keyword arguments and return values for the owner to assign.

## Verify

- Compile every changed module.
- Run the focused tests for the moved responsibility.
- Run the full pytest suite after the batch.
- Inspect `git diff --stat`, moved-symbol call sites, and remaining line counts.
- Report before/after file and function sizes plus any behavior that could not
  be proven unchanged.
