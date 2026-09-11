# Puffo Agent Engineering Rules

These rules apply to the entire repository.

## Python Design

- Keep every tracked Python file at or below 2,000 lines and every function or
  method at or below roughly 100 lines. Existing violations are active
  refactoring work, not permanent exceptions.
- Prefer stateless, pure functions and immutable values. Put necessary mutation
  in an explicit lifecycle owner such as `MessageStore`, `SendCoordinator`, a
  Driver session, `GlobalInboxRuntime`, or `Worker`.
- Keep composition roots and orchestration functions short enough to read like
  pseudocode. Move parsing, transformation, policy, and persistence into
  cohesive collaborators.
- Pass the values a callee needs, preferably by keyword. Do not pass a large
  client, worker, config, or runtime merely to let a helper reach through it.
- Avoid behavioral mixins that obscure ownership and hidden cross-module
  writes. A compatibility facade may use private implementation traits only to
  split one documented state owner; those traits must not own an independent
  lifecycle or be reused by another facade. Otherwise prefer composition and
  return a result for the state owner to assign.
- Default new methods to private or protected. Add public surface only for a
  real cross-module contract.
- Dataclasses are allowed. Use `frozen=True` for snapshots and protocol values
  that should not change; use mutable dataclasses only for explicit state
  owners.

## Contracts And Compatibility

- Access required contract fields directly. Do not use defensive `getattr` or
  `hasattr` to hide a producer/consumer mismatch.
- Dynamic access is allowed only at a named compatibility, plugin, CLI, or test
  boundary. Normalize there once into a typed internal shape.
- When a field or API changes, trace every producer, persistence boundary,
  serializer, transport, and consumer. Keep compatibility policy at the
  boundary rather than scattering fallbacks through business logic.
- Do not add a Driver, MCP, or test API that merely forwards one field. New
  surface must provide a real control primitive, stable contract, or reusable
  multi-source derivation.

## Change Discipline

- Separate mechanical moves from behavior changes. A file split or symbol move
  preserves signatures, ordering, exceptions, logging, and observable output.
- Do not combine broad formatting, module moves, and logic changes in one
  review unit.
- Keep tests proportional to the guarded failure modes. Follow the additional
  rules in `tests/AGENTS.md` for every test change.
- Run the narrowest relevant tests while iterating, then the full suite before
  declaring the change complete.
- Run `python tools/check_python_structure.py` after structural edits. The same
  limit check runs in pre-commit and CI.

## Required Skills

- Large file, large function, module extraction, or class split:
  `.agents/skills/refactor-puffo-python-components/SKILL.md`
- Adding, deleting, or restructuring tests:
  `.agents/skills/write-puffo-python-tests/SKILL.md`
- Driver, MCP, WS-local, Inbox, context, or send contract changes:
  `.agents/skills/evolve-puffo-runtime-contracts/SKILL.md`
- Puffo-owned environment variables:
  `.agents/skills/manage-puffo-env-vars/SKILL.md`
