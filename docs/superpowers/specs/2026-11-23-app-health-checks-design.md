# App-declared health checks (aeth-ext + devkit-container)

## Why

IMAPReportCollector crashed in production twice (2026-11-22, 2026-11-23) when its IMAP server
stopped answering. The app-side fix (reconnect with backoff instead of crashing) is done in that
repo. What it lacks is a way to say "I'm alive but a dependency is down": today the only health
signal is `heartbeat.txt`, which means "the process is alive". Apps fake finer signals by
withholding that file (timeclock-system stops its heartbeat while a fingerprint-reader call
hangs), which conflates a hung app with a degraded dependency.

This feature lets an aeth-ext app declare named health checks that it pings, like healthchecks.io
checks, and choose per check what happens when one stops arriving. devkit-container reads the
declarations and acts on them.

## Goals

- An app declares checks through aeth-ext, each with a period, a grace period, an optional stall
  message and three flags. It then pings each check with one call,
  `send_health_check(name, failure=False, message=None)`.
- A check **stalls** when no ping lands within its period plus grace. It is **failing** while it
  is stalled or its latest ping was a failure ping.
- A failure ping (`failure=True`) always sends a healthchecks.io `/fail` carrying its message,
  whatever the flags, because the call asked for it explicitly.
- The three per-check flags set what else happens:
  - `affects_ping`: while the check is failing, the regular healthchecks.io ping is withheld.
  - `fail_ping`: when the check stalls, a `/fail` is sent for it, regardless of every other check.
  - `shutdown`: when the check stalls, the app is stopped. This sends no ping of its own; set
    `fail_ping` as well for that.
- aeth-ext declares one built-in check, `heartbeat`, automatically during startup; the heartbeat
  pings it. Consumers that won't run the heartbeat opt out.
- Whoever owns the healthchecks.io ping applies the ping rules: the supervisor when supervised,
  aeth-ext otherwise, the way the heartbeat ping already works.
- `shutdown` is applied by the supervisor when supervised, and by aeth-ext otherwise, but only
  when `__debug__` is off. Local runs keep `__debug__` on, so a paused debugger can't stop them.
- Every declared check feeds Docker health (`devkit-container healthcheck`), with the app's
  message as the reason, so Coolify shows the container unhealthy, not exited.
- An app that opted in (the startup timeout below) but hasn't written its declarations in time is
  alerted on and stopped, since that always needs immediate attention.
- Apps that declare no checks behave exactly as today.

## Non-goals

- Changing `heartbeat.txt`: its format, 180 s max age and its role in the ping all stay.
- Alert emails or any notification channel beyond healthchecks.io.
- Changing the compose template's healthcheck command.

## Anti-goals

- A dependency outage must not look like the program went down unless the app opted into that
  through the flags or a failure ping.
- No silent tolerance of contract misuse: declaring after the first ping, or pinging an undeclared
  check, raises.

## Constraints

- The compose template pins the healthcheck `test:` list exactly (`# !rule exact-list`), so
  devkit-container must discover checks by itself, from a fixed location.
- `healthcheck` is a fresh process on every Docker probe (every 30 s). It reads files only: no
  root, no pyproject parse, no dependence on the supervisor (existing rule, kept).
- Seven programs use the heartbeat API today (IMAPReportCollector, ScheduledInvoiceProcessor,
  ScheduledReportAggregator, timeclock-system, timeclock-backend, wireguard-hub,
  wireguard-spoke-agent). None may need changes.
- aeth-ext already depends on devkit-container (`>=2.3.1`), so aeth-ext's release can raise that
  floor and guarantee the reader exists wherever the writer does.
- Process: each library gets its own feature branch (`feat/app-health-checks`) and PR. Nothing
  merges until the user has reviewed it. Each PR gets two rounds of GitHub Copilot review plus
  fixes, on top of our own reviews.

## Approaches

The user set the architecture: aeth-ext writes a one-time declarations file plus per-check state;
devkit-container reads them, with the supervisor caching the declarations. Alternatives considered
and rejected:

- **Extra `--file` heartbeat files listed in compose.** The template locks the command list, and a
  bare timestamp can't carry a message or flags.
- **Gating `heartbeat.txt` on dependency health.** No compose change, but a dependency outage then
  reads as a hung app, and there is nowhere to put a message.
- **A verdict file written by the supervisor, read by `healthcheck`.** It would let the probe skip
  reading declarations, but Docker health would then depend on the supervisor. Reading one small
  JSON file per probe is cheap.

## The file contract

Everything lives in `persisted_data/logs/health/` (`<app_root>` defaults to `/app`), beside the
heartbeat files on the host bind mount, so another container mounting it read-only can judge the
same files.

| File | Writer | When |
| --- | --- | --- |
| `checks.json` | aeth-ext | once, on the first ping of any check (normally the built-in `heartbeat`, right after boot) |
| `state/<name>.json` | aeth-ext | on every ping of that check |
| `declare-by.txt` | `devkit-container run` | at start, only when the startup timeout is configured |

`checks.json`:

```json
{
  "version": 1,
  "written_at": "2026-11-23T14:00:00.123456+00:00",
  "checks": {
    "imap": {
      "period_secs": 600, "grace_secs": 120, "stall_message": "IMAP server unresponsive",
      "affects_ping": false, "fail_ping": false, "shutdown": false
    }
  }
}
```

`state/<name>.json`: `{"at": "<ISO 8601>", "failure": false, "message": null}`; a failure ping has
`"failure": true` and its message.

`declare-by.txt`: one ISO 8601 timestamp (container start + timeout), in the heartbeat format.

All timestamps are `datetime.isoformat()` with an offset, parsed by the existing
`heartbeat::parse`. Every write is atomic (sibling temp file, then rename in the same directory),
so a reader sees a whole file or none.

**Lifecycle.** `devkit-container run` removes a leftover `health/` folder at every start,
supervised or not, so a previous run's declarations or failures never apply to this one. When the
startup timeout is configured it then recreates the folder (owned by nonroot) and writes
`declare-by.txt`. aeth-ext creates the folder itself if it is missing (a local run, or no timeout
configured). A state file older than `checks.json`'s `written_at` is ignored, which covers local
runs that never pass through `run`.

**The built-in check anchors the write at boot.** A check that only pings on success (IMAP's)
doesn't ping at all while its dependency is down. The built-in `heartbeat` check pings on the
heartbeat's first tick, right after boot, so that ping freezes the registry and writes
`checks.json`. A dependency down at boot therefore can't trip the startup deadline. The deadline
only catches an app whose heartbeat never got going, and a check that never pings stalls from
`written_at` like any other.

**Read guards.** Atomic writes are the main guard. A reader also validates what it reads: JSON
that doesn't parse, an unknown `version`, or a missing field is reported as unreadable. The probe
reports that as a failure; the supervisor logs it once, leaves the cache empty and retries on its
next poll.

## Judging a check

One rule, applied by `healthcheck`, the supervisor and (for its own duties) aeth-ext. At time
`now`, for a declared check with period `P` and grace `G`:

1. The clock starts from the latest current ping's `at`, else from `checks.json`'s `written_at`
   (declared but not pinged yet).
2. **Stalled** if `now - clock >= P + G`: `<name>: <stall_message> (no ping for N s)`, or
   `<name>: no ping for N s (period P s + grace G s)` without a stall message.
3. Else **failing** if the latest ping was a failure ping: `<name>: <message>`, or
   `<name>: failure ping` when the message is empty.
4. Else **healthy**.

A stall wins over an older failure ping, since it is the more recent fact. The judgment keeps the
two causes apart, because only stalls trigger `fail_ping` and `shutdown`.

**Before `checks.json` exists:** healthy, unless `declare-by.txt` exists and `now` has passed it,
in which case it fails with `health checks not declared by <timestamp>`. The supervisor then
alerts and stops the app (below); unsupervised, only Docker health shows it.

## aeth-ext

New module `aeth_ext/monitoring/health.py`, exported from `aeth_ext.monitoring`:

```python
declare_health_check("imap", period=600, grace=120, stall_message="IMAP server unresponsive")

send_health_check("imap")                                              # healthy
send_health_check("imap", failure=True, message="mailbox unreadable")  # failure ping
```

### Registry and pings

- `declare_health_check(name, *, period=UNSET, grace=UNSET, stall_message=UNSET,
  affects_ping=UNSET, fail_ping=UNSET, shutdown=UNSET)` registers a check in a process-wide
  registry. Every parameter defaults to a sentinel object, so the call can tell "not given" from
  `None`:
  - A new name takes the values given. `period` and `grace` are required; the rest default to no
    stall message and every flag off.
  - Re-declaring a name before the registry freezes overwrites only the values actually passed;
    the others keep what was declared before. This is also how an app adjusts the built-in check.
  - An explicit `None` voids a value, resetting it to that parameter's own default (not to the
    previously declared value): `stall_message=None` removes the message, and a flag set to
    `None` goes back to `False`. A parameter with no default (`period`, `grace`) can't be voided.
  - `period` and `grace` are whole seconds (`int`), like every `*_SECS` value.
  - It raises when the name is invalid (`[a-z0-9][a-z0-9_-]{0,63}`, since it becomes a file
    name), when the registry has frozen, when a new name lacks `period` or `grace`, or when
    `period < 1`, `grace < 0`, or either is voided.
- `send_health_check(name, *, failure=False, message=None)` pings a check. It first does a fast
  registry lookup and raises if `name` isn't declared. It writes that check's state file and, for
  a failure ping, triggers the `/fail` (below).
- The first ping of any check freezes the registry and writes `checks.json`. A ping's `at` is
  taken after `written_at`, so the first ping is never ignored as older than the declarations.
- **The built-in `heartbeat` check** is declared by `initialize()`, before any app code declares
  its own. `initialize(heartbeat_check=False)` opts out, following the existing boolean opt-outs
  there (`install_signal_handlers`, `install_crash_hooks`). It mirrors today's heartbeat: period
  60 s (`DEFAULT_HEARTBEAT_INTERVAL_SECS`), grace 120 s (today's 180 s max age), `affects_ping`
  and `fail_ping` on, `shutdown` off. The shared heartbeat primitive (`_send_heartbeat`) pings it on
  every tick, so every scheduler, apps' own loops included, keeps it fresh. A scheduler with a
  custom interval re-declares `heartbeat` with that period before its first tick, while the
  registry is still open; apps change its other settings the same way. `heartbeat.txt` is still
  written, since other readers use it.
- The fatal contract errors raise a dedicated exception type; aeth-ext's crash hooks make it fatal
  wherever it is uncaught.
- File writes catch `OSError` only (the project allowlists observed exception types rather than
  catching broadly); those are logged and never raised, as the heartbeat does, because a health
  ping must not take the process down. If writing `checks.json` failed, the next heartbeat or ping
  retries it.
- One lock covers the registry and the writes; pings may come from any thread.
- Messages are collapsed to a single line and capped (500 characters) when written, so every
  reader shows the same text.
- Paths come from `log_loc_folder` (`persisted_data/logs`), like `heartbeat.txt`.

**Duties when unsupervised.** `DEVKIT_SUPERVISED_PING` unset means no supervisor owns the ping;
then aeth-ext applies the rules itself, inside the shared heartbeat primitive, so every scheduler
gets them:

- The regular ping is skipped while any `affects_ping` check is failing. `heartbeat.txt` is still
  written, since it means "alive".
- A `fail_ping` check's transition to stalled sends `/fail` with its stall reason.
- A failure ping sends `/fail` with its message, off the caller's thread, so a ping never blocks
  on the network.
- With `__debug__` off, a `shutdown` check's transition to stalled logs the reason and calls
  `run_shutdown(ShutdownKind.GRACEFUL)`, the same shutdown a SIGTERM starts.
- Pings go out only when a ping URL is configured, as today.
- `ping_healthcheck` gains an optional body (sent as POST, as devkit-container does), so `/fail`
  carries the reason.

Version: 10.2.0, with the devkit-container floor raised to the release that reads these files.

## devkit-container

New pure module (call it `checks.rs`): parse `checks.json`, state files and `declare-by.txt`, and
apply the judging rule with an injected `now`. Unit-testable without a container.

**`pyproject.rs`:** new optional key `[tool.docker].health_checks_timeout_secs`, a whole number
from 1 to 31536000 like the other `*_SECS` values. Absent (the default) means no deadline. Setting
it is the opt-in.

**`run`:** removes `persisted_data/logs/health/` at every start, before the startup scripts. With
the key set, it recreates the folder owned by nonroot and writes `declare-by.txt`, then passes the
timeout to the supervisor.

**`healthcheck`:** keeps its `--file` checks unchanged, then always judges
`<app_root>/persisted_data/logs/health/` with the shared rule, printing one stderr line per
problem. With no `health/` folder it behaves as today.

**Supervisor**, on each poll (`WG_POLL_SECS`, default 30):

- **Declarations:** while not cached, try to read `checks.json`. Once it is valid, cache it and
  never read it again. Without the pyproject key the supervisor still reads and enforces them;
  only the deadline is skipped. The key does not turn on `supervise`.
- **Declarations past the deadline:** log it, send `/fail` (`health checks not declared within
  N s`), stop the app as `shutdown` does, and exit 1. Return before the generic "nonzero exit →
  `/fail` with the code" step, so one alert goes out, not two.
- **State:** read every declared check's state file and judge it. An unreadable state file counts
  as failing, as in the probe. Each poll, track per check whether it is stalled, for transitions.
- **Regular ping.** `Pinger` gets two inputs:
  - `core_fresh`: the app heartbeat and the tunnel heartbeat. These keep today's behaviour:
    plain while fresh, `/fail` once on the transition to stale.
  - `withheld`: any `affects_ping` check is failing. While withheld, no plain ping goes out and no
    `/fail` is sent on its account; plain resumes on recovery. `/start` waits until the ping is
    fresh and not withheld.
- **Failure pings:** every new failure ping (`failure: true`, with an `at` newer than the last
  poll's) sends `/fail` with its message, whatever the flags. Several between two polls send one.
- **`fail_ping`:** on a check's transition to stalled, send `/fail` with its stall reason.
- **One `/fail` per poll:** if several of these (or a core stale transition) call for `/fail` in
  the same poll, send one whose body has one line per reason, because the ping sender allows one
  request in flight. A `/fail` replaces that poll's plain ping. Without `affects_ping`, the next
  poll's plain ping turns healthchecks.io back to up, so a check that keeps sending failure pings
  makes it flip down and up on each one. Set `affects_ping` to hold it down until recovery.
- **`shutdown`:** on a check's transition to stalled, log the reason, stop the app and the daemons
  with SIGTERM (30 s, then SIGKILL), bring the tunnel down if there is one, and exit 1. Like the
  other stop paths, it returns before the generic exit-code ping, so no ping goes out unless
  `fail_ping` sent one first. Wait for that in-flight ping before returning.
- **During a stop:** once a SIGTERM or SIGINT has been forwarded to the app, check transitions are
  ignored, so a stopping app's checks can't start a second stop or send alerts.
- Before its first judgment a check counts as not stalled, so a stall on the first judgment is a
  transition.

**Cargo:** `serde_json` moves from the Unix-only dependencies to the general ones, since
`healthcheck` also builds on Windows.

**README:** `run` (folder handling), `healthcheck`, the supervisor's ping and stop rules, the
`[tool.docker]` table, and "Heartbeat files" (now covering `health/`).

Version: 2.4.0. The compose and Dockerfile templates are unchanged.

## First consumer: IMAPReportCollector

Not part of either PR; done in that repo after both are released:

- Declare `imap` at module level with period = `WATCH_POLLING_TIMEOUT_SEC` (one connect/fetch/IDLE
  cycle), grace 120 s, `stall_message="IMAP server unresponsive"` and no flags. Its stall only
  marks the container unhealthy in Coolify: no healthchecks.io effect, no shutdown.
- Ping it (success only) after login plus the initial fetch. It sends no failure pings. An outage
  stops the pings, and the check stalls after period plus grace: about 12 minutes with the
  production 600 s IDLE, plus Docker's three 30 s retries. A shorter IDLE period shortens that.
- Set `[tool.docker].health_checks_timeout_secs`. The built-in `heartbeat` check's first ping
  writes the declarations at boot, so IMAP being down at boot can't trip it.
- Raise its `aeth-ext` floor to 10.2.0.

## Delivery and release order

- Both PRs open in parallel. The aeth-ext PR leaves out the `devkit-container>=2.4.0` floor and
  its lock update, since the lock can't resolve 2.4.0 until it is published.
- Once the user has merged and released devkit-container 2.4.0, the floor bump becomes the aeth-ext
  PR's last commit; aeth-ext 10.2.0 is merged and released after that.
- The user runs the releases (`devkit release`), since publishing reaches every project that locks
  these packages.
- IMAPReportCollector's wiring lands once 10.2.0 is published.

## Decided by the user

- The architecture: declarations file written by aeth-ext once; register everything before any
  ping; pinging an undeclared check, or declaring after the registry froze, crashes.
- One ping call per check with a `failure` keyword; a failure ping always sends `/fail`, whatever
  the flags.
- Per-check period and grace. `affects_ping` decides whether a failing check withholds the
  regular ping; `fail_ping` sends `/fail` only when the check stalls; `shutdown` sends no ping of
  its own.
- devkit-container treats a missing declarations file as healthy up to a configurable timeout,
  opted into through pyproject and absent by default. Past it, the supervisor alerts and stops the
  app.
- The supervisor reads the declarations once, as soon as they exist, with mid-write guards, and
  caches them. `healthcheck` rereads them on every probe.
- aeth-ext keeps pinging when unsupervised, applying the check rules itself; its in-process
  `shutdown` only applies with `__debug__` off. `DEVKIT_SUPERVISED_PING` alone tells it whether a
  supervisor owns the pings.
- aeth-ext declares a built-in heartbeat check automatically during startup, and it counts as a
  check ping; consumers that won't fire it opt out.
- Each check can carry its own stall message.
- Before the registry freezes, re-declaring a name overwrites only the values passed in that
  call; a sentinel default lets an explicit `None` void a value, resetting it to the parameter's
  own default. Parameters without a default can't be voided.
- IMAPReportCollector's check only marks the container unhealthy in Coolify; it sends no failure
  pings.
- The PR process under Constraints; sequencing left to the implementer.

## Calls made while writing this (not yet confirmed by the user)

- The built-in check is named `heartbeat` and mirrors today's heartbeat (period 60 s, grace
  120 s, `affects_ping` and `fail_ping` on, `shutdown` off); the opt-out is
  `initialize(heartbeat_check=False)`.
- API names `declare_health_check` / `send_health_check`, and the stall message as a
  `stall_message` field in `checks.json`.
- A check is failing while stalled or after a failure ping until its next success ping.
- File layout under `persisted_data/logs/health/`, the JSON shapes, atomic writes, and ignoring
  state older than the declarations.
- `run` clears `health/` at every start; the timeout reaches `healthcheck` through
  `declare-by.txt`, so the probe still never parses pyproject.
- Every declared check feeds Docker health; the flags only add ping and shutdown reactions.
- `shutdown` acts on stalls, like `fail_ping`. The supervisor stops with SIGTERM and exits 1,
  mirroring the broken-tunnel path; aeth-ext uses `run_shutdown(GRACEFUL)`.
- Several failure pings between two polls send one `/fail`.
- File writes catch `OSError` only; contract misuse raises a dedicated exception.
- Messages collapsed to one line and capped at 500 characters.
- Whole-second periods; the key name `health_checks_timeout_secs`; versions 2.4.0 and 10.2.0.

## Left to the builder

Module, type and function names beyond the public API above; how JSON is parsed in Rust; test
layout, following each repo's existing suites; README wording; the exact log lines.
