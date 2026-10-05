# Daemon instance identity

A PID is a reusable process number. Matching `puffo-agent start` in its command
line does not prove it owns the current Puffo home. Stale PID and readiness files
could otherwise suppress startup when another Puffo process inherits that PID.

New daemons publish these local markers:

- `daemon.pid`: the numeric PID, retained for older stop clients.
- `daemon.identity.json`: version 1, PID, process creation time, canonical Puffo
  home, and a random instance ID generated for each daemon run.
- `daemon.ready`: the same identity record once startup completes.
- `.stop_requested`: the existing PID/timestamp envelope plus an `identity`
  record. Readers compare the whole record to the daemon instance being stopped.

Process checks verify the daemon command, process creation time and process home.
The process home comes from its environment (`PUFFO_AGENT_HOME`, or the platform
home plus `.puffo-agent`), with relative overrides resolved against its cwd.
An inaccessible process raises an ownership verification error rather than being
reported absent and allowing an unverified second daemon to start.

The stop client captures one identity and polls that process birth, even when a
successor replaces the marker files. Ready publication and normal shutdown
cleanup carry the owner's instance identity so a prior run cannot mark a
successor ready or clear its files merely because the PID matches.

## Upgrade compatibility

A running daemon without an identity sidecar is treated as legacy only after
verifying its command, its home, and that it was born no later than the PID
file's modification time. Numeric legacy ready markers must also postdate that
process's birth. An existing invalid sidecar never falls back to legacy checks.

A new stop client can stop an old daemon: its JSON request retains the top-level
PID. An old CLI can stop a new daemon with either a PID-only JSON or the older
scalar sentinel, provided the file was written after the new daemon's PID
publication. Startup still clears preexisting stop requests. Such legacy
requests cannot carry an instance ID; their compatibility guarantee is bounded
by file publication time, not the stronger new-client identity protocol.

Old CLIs do not understand the structured ready marker. Use the upgraded CLI to
observe readiness. Before downgrading, stop the daemon with the current CLI;
normal shutdown removes the identity sidecar along with the PID file.

This change does not add a cross-process startup lock or defend marker files
against a malicious process with the same filesystem permissions. It prevents
accidental PID reuse and cross-home ownership confusion.
