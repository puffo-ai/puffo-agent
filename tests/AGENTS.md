# Puffo Test Admission Rules

Every test must name the future production regression that would make it fail.
If deleting the test leaves no silent failure path, delete the test.

A new test must protect at least one of these:

1. A reproduced bug. Observe the test fail on the broken implementation, then
   pass after the fix. Describe the mechanism in behavioral terms.
2. A derived property or protocol invariant, such as ordering, idempotency,
   routing, encryption boundaries, lifecycle transitions, or retry semantics.
3. Critical bookkeeping that can silently drift, such as field propagation,
   serialization compatibility, registry completeness, or config migration.
4. A cross-component workflow whose failure cannot be detected at a smaller
   boundary. Keep these integration cases few and representative.

Do not add:

- Happy-path assertions that mirror an implementation line.
- Multiple cases guarding the same failure mode.
- Probabilistic concurrency stress that does not fail on the broken code.
- Test-only production APIs that expose private state without a real runtime
  use case.
- Verbatim prompt snapshots unless exact text is itself a public contract.

Use deterministic scheduling for concurrency regressions. Mock network, clock,
provider, and filesystem boundaries, not the logic under test. Prefer an
existing test module and fixture over another near-duplicate test file.

Keep test files at or below 2,000 lines and test functions at or below roughly
100 lines. Split by guarded behavior, not by arbitrary line ranges.
