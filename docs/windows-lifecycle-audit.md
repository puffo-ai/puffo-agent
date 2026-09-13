# Windows lifecycle audit — 2026-09-13

Scope: native Windows, local CLI agents, Python 3.11, including a uv-managed
installation and a standard virtual environment. This is an audit of lifecycle
owners and their OS boundaries, not a claim that every provider and transition
has passed a live end-to-end test. macOS/Linux were not investigated here.

## Confirmed failures addressed

* Background launch: the uv interpreter trampoline could reopen a console;
  use the base windowless interpreter with the virtual environment preserved.
  Preserve redirected logs and process/job detachment. Native coverage checks
  the interpreter prefix, imports and absence of a console.
* CLI initialization: Windows normalizes `SystemRoot` to `SYSTEMROOT`; dropping
  it from the child environment could crash the Node launcher before startup.
* Codex shutdown: terminating only the wrapper left Node/native descendants
  alive. Codex now uses isolated, bounded process-tree shutdown. Windows
  `taskkill /T /F` targets the tree before its root disappears, rather than
  orphaning descendants after an unsuccessful non-forceful taskkill.
* Archive/delete staging: copying the entire directory encountered exclusive
  file locks and MAX_PATH, slept for 33 seconds per exhausted copy phase,
  and could overwrite an existing destination. Windows now attempts an atomic
  directory rename off the event loop. Failure preserves the original and its
  request flag for the next reconciliation tick. No cross-volume copy fallback
  is attempted; unusual cross-volume/reparse layouts fail visibly and safely.
* Archive contents: Windows no longer deletes provider tmp files as a
  prerequisite. Exclusive locks still need to close before rename can succeed.
* Completion: `archived` is reported after a successful local move, using the
  relocated keystore, and before device revocation. A transient heartbeat or
  revoke failure leaves a durable retry marker; startup retries the heartbeat
  before revoking. Existing markers without a lifecycle field remain valid.
* Final deletion: extended Windows paths allow recursive removal after moving
  a tree to the longer archive path. Filesystem work runs off the event loop.

## State and operation inventory

| Owner / states | Operations and Windows boundary | Evidence / remaining limits |
| --- | --- | --- |
| Daemon startup: `starting`, `ready`, `exited` | background start, duplicate start, stop; console, job and PID ownership | Native background tests pass; live restart reconnects. Login autostart is a separate path below. |
| Agent desired state: `running`, `paused`; observed runtime: `starting`, `running`, `paused`, `error`, `stopped` | create, pause, resume, config restart, restart flag | Control/worker tests pass. Codex tree cleanup fixes the shared shutdown boundary. Warm-up remains serial with a 120-second per-worker bound. |
| Archive/delete flags → moved directory → report/revoke → optional removal | directory rename, provider locks, long paths, network retry | Native locked-file, long-path, destination-collision and deletion tests pass. Two live pending archives completed; both devices were revoked. |
| Runtime health: `unknown`, `ok`, `in_progress`, `auth_failed`, `api_error_abandoned`, `provider_error`, `refresh_broken`, `drained`, `extra_usage_required`, `unhandled_error`, `codex_thread_wedged`, `server_unreachable`, `mcp_unreachable`, `no_progress` | heartbeat, quota/auth gating, health probes, reopen | Worker/health tests exercised on Windows. Network/provider failures were not all induced against live services. Health is separate from lifecycle/activity. |
| Runtime/session: ready, failed, exited; session opened/resumed/updated | start, close, refresh, resume fallback; pipes and child processes | Driver and runtime tests cover these paths; native Codex descendant cleanup passes. A killed/already-exited launcher can still make descendants undiscoverable by PID-tree lookup; Job ownership would be a stronger future guarantee. |
| Turn/tool/permission events | submit, steer, cancel, approve/reject, terminal completion/abandonment | Runtime/command tests run on Windows. No live permission or cancellation was injected into user conversations. |
| Context/compaction: started, completed, failed | context updates, automatic/manual compaction | Tests exercise state handling. A baseline test assumes `/` separators; this is not evidence of a product state failure. |
| Turn recovery: `stop_attempted`, `stopped`, `retry_requested`, `resolved` | inspect, stop, explicit retry after unknown effects | Persistence uses atomic writes and excludes POSIX directory fsync on Windows. Recovery tests run; no deliberate live crash during a user turn. |
| Inbox: `pending` → `in_turn` → `processed`; receipt `committed` / `idempotent` / `conflict` | admit, retry, resume, reminders, durable completion | Message-store and Inbox tests run on Windows. Timing-sensitive baseline tests can fail under load; no new Inbox algorithm changes here. |
| WS/control/local attachment | connect, disconnect, reconnect, command acknowledgment, session close | WS-local/transport tests run. A baseline path-swap test tries to unlink an open file, which Windows denies. Server command acceptance remains distinct from local completion; web frontend was not changed. |
| Credentials/import/revoke | refresh, import, archive retry, device revoke | Import/revoke regression verifies that failed reporting preserves credentials for retry. Unix permission-bit assertions are not portable to Windows ACLs. |
| Login autostart | HKCU Run enable/disable/status, interpreter upgrade | Static audit: separate same-venv `pythonw.exe` selection can encounter uv trampoline behavior. Run entries are unsupervised and do not restart crashes. Login/logoff not tested or reconfigured. |

## Other follow-up risks

* `Worker.stop()` logs adapter/client cleanup failures and still writes
  `stopped`; that state alone does not prove every resource was released.
  Native process-tree cleanup reduces the reproduced cause, but cancellation
  and cleanup-failure ownership need a separate design/test pass.
* Lifecycle reconciliation still awaits worker shutdown and network reporting.
  Removing bulk copies and fixed archive sleeps fixes the observed long stall;
  it does not make independent agents' lifecycle transitions fully concurrent.
* Pending archive network retries currently run at startup. Files remain safe,
  but a continuously running daemon does not periodically sweep these markers.
* Docker, remote/WS-local providers and login autostart were not exercised as
  real Windows deployments. No registry or login configuration was changed.
* A parent Windows Job that prohibits breakaway rejects detached startup with
  WinError 5. Hosted GitHub runners impose this restriction: CI exercises the
  native console/environment probe inside that Job; the separate breakaway
  variant runs locally. Production detachment flags are not weakened.

## Validation

The three native regressions first failed on the previous implementation:
locked long-path archive, existing destination preservation, and a surviving
Codex wrapper descendant. They pass after the fixes. A standard-venv run of
the new Windows CI selection completed with **178 passed, 3 POSIX-only skips**.
The broader focused lifecycle/import/driver run completed with **177 passed**
(excluding the known Unix mode-bit import assertion).

A separate state/operation test selection completed **881 passed, 4 failed**.
All four failures also reproduce on the pre-change baseline: a slash-only
assertion, default Windows text decoding, a 50 ms timing expectation, and
unlinking an open WS-local protocol file. These are recorded, not hidden by
changing production behavior to satisfy the tests.

The full Windows suite is not currently a green gate: baseline failures and
a test that changes the platform globally cause pytest itself to abort with
`cannot instantiate PosixPath on your system`. Native Windows lifecycle jobs
are added for Python 3.11 and 3.12 alongside the existing Ubuntu full-suite CI.

Live verification: the old long-path archive moved in approximately **12 ms**
(local move only), retaining 5,508 files / 107,402,173 bytes. The second archive
retained 259 files / 65,306,716 bytes. Neither remained in active agents, both
pending-revoke markers cleared, and the other five configured agents resumed
running. Prior partial archive copies were not deleted.
