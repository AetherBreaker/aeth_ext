# Unconditional shutdown exit, and a confirm-then-force signal ladder

Design agreed 2026-08-11, on branch `feat/two-phase-logging-config`.

## Context

PR #11 review comment [`r3762403632`](https://github.com/AetherBreaker/aeth_ext/pull/11#discussion_r3762403632)
reports that `_handle_shutdown_signal` can permanently swallow SIGINT/SIGTERM. The finding is
correct, but the underlying problem is larger than the one it names, and the fix it proposes is
weaker than what the situation allows.

### Two defects, not one

**A — permanent swallow.** `_early_exit_armed` is set only by `_attempt_early_exit`, which is
reached only when `exit_when_done=True`. After any background `_handle_fatal` (the default is
`False`), the flag stays `False` forever. Every later signal falls through to
`run_shutdown(..., exit_when_done=True)`, which is silently no-op'd by the
`next(_drive_counter) != 0` early return. The signal is discarded, permanently.

**B — the in-flight window.** Even with A fixed, `_early_exit_armed` is `False` for the whole
threaded pass (up to the 7s graceful budget). A signal in that window is swallowed the same way.
This is the *normal* case, not the buggy one, and it is precisely when an operator presses again —
"the shutdown is taking too long" is a seven-second complaint. It breaks the promise made at
[shutdown.py:439-441](../../src/aeth_ext/errors/shutdown.py#L439-L441).

### Why `exit_when_done` is being deleted rather than repaired

The parameter's justification at [shutdown.py:522-527](../../src/aeth_ext/errors/shutdown.py#L522-L527)
does not hold up:

- It claims exiting would break the decorators' "swallow and return `None`" contract by hijacking
  the calling thread. It would not — `_attempt_early_exit` runs on a separate daemon thread and
  nudges the *main* thread, whoever called `run_shutdown`. The decorator still returns `None`.
- It claims consumers watching `SHUTDOWN` unwind on their own. That is conditional on someone
  watching, and a wedged loop is exactly the failure this machinery exists to survive. The D-I4
  early exit was built because that hope is not a guarantee.

The decisive point is one the review does not make: **`exit_when_done=False` does not skip the
shutdown, only the exit.** Every teardown callback still runs, including the logging transport at
`LOGGING_TRANSPORT_PRIORITY`. The `False` path deliberately produces a process that is alive, fully
dismantled, and unable to report anything about itself — worse than either running or exiting.
There is no coherent "shutdown that doesn't shut down", so the flag has exactly one correct value
and should not exist.

API breakage is not a concern: unmerged branch, no consumers.

## Decisions

| Decision | Rationale |
| --- | --- |
| `__debug__` gate on `install_shutdown_signal_handlers` **stays** | Handlers remain `-O`-only. Dev Ctrl-C stays stock Python. |
| The confirm ladder is a **guard on the force path**, not a dev affordance | It must exist wherever force is reachable, independent of who is at the keyboard. Justifies itself under `-O` alone, and covers a future non-Docker `-O` deployment with a terminal. |
| Ladder has **four rungs**, ending in a hard interrupt | `FORCED` can only drop callbacks *between* them. A wedged `required` callback — e.g. the thread join at [writer_thread.py:179-184](../../src/aeth_ext/central_log_server/server/writer_thread.py#L179-L184) — needs a terminal answer. |
| Shutdown's own output is **raw-fd only**, never logged | See "Reporting policy". |
| Exactly **one** `logger.critical` marker, in the threaded pass | See "The one log record". |
| Diagnostics go to **fd 2**, not fd 1 | See "Reporting policy". |
| The `emergency_fd` handler protocol is **out of scope** | Deferred to [TODO.md](../../TODO.md) item 8. The design here needs no rework to adopt it later. |

## State model

**Deleted:** `exit_when_done` from `run_shutdown`, `_run_threaded_pass`, `_handle_fatal`,
`trigger_shutdown`, and `report_exc`; the `if not exit_when_done: return` at
[shutdown.py:377-378](../../src/aeth_ext/errors/shutdown.py#L377-L378); and the docstring block at
[shutdown.py:510-527](../../src/aeth_ext/errors/shutdown.py#L510-L527) that existed only to justify
it. The threaded pass now always ends in `_attempt_early_exit()`.

`trigger_shutdown`'s `kind` parameter is kept — it still meaningfully selects the budget.

**`ShutdownKind` gains a fourth member:**

```python
RUNNING = 0; GRACEFUL = 1; FATAL = 2; FORCED = 3
_BUDGETS = {GRACEFUL: 7.0, FATAL: 1.0, FORCED: 0.0}
```

`ShutdownState` gains a third one-shot `_forced` event; `kind` checks `FORCED → FATAL → GRACEFUL`,
preserving the existing property that a read racing a request can only observe the *newer* state;
`request()` dispatches three ways. The lock-free, monotonic-by-construction design is untouched —
that is what makes `request()` safe to call from a signal handler, and it scales to N kinds free.

`FORCED` needs no new skip mechanism. `_run_threaded_pass` already re-reads `SHUTDOWN.kind` every
iteration ([shutdown.py:367](../../src/aeth_ext/errors/shutdown.py#L367)) so a mid-pass escalation
shrinks the remaining budget; a zero budget therefore skips every non-`required` callback from that
moment while the four `required=True` transport arms/joins still run. `max()`-escalation means it
correctly outranks an in-flight `FATAL`.

One correctness fix this forces: the budget comparison at
[shutdown.py:368](../../src/aeth_ext/errors/shutdown.py#L368) becomes `>=` rather than `>`, so a
zero budget actually skips the first callback instead of depending on the clock having ticked.

**Flags:**

| Now | Becomes | Why |
| --- | --- | --- |
| `_early_exit_armed` | `_exit_nudge_sent` | Meaning narrows to "our own `interrupt_main()` is in flight". It no longer doubles as the escape-hatch arming signal. It cannot be deleted: `interrupt_main()` simulates SIGINT and so re-enters our own handler, which must tell that apart from an operator keypress. |
| — | `_confirm_counter = count()` | Per-signal ticket for the ladder. Same lock-free `next()`-under-GIL idiom as `_drive_counter`, for the same interrupt-context reason. |
| — | `_t0: float` | Set by the first driver in `run_shutdown`. Origin for both the elapsed display prefix and the threaded pass's budget, replacing its local `started`. |
| `_drive_counter` | unchanged | Still needed — `run_shutdown` has non-signal callers needing their own first-mover test. |

Folding the budget onto `_t0` includes the interrupt pass in the measurement. That pass is atomic
stores by construction, so the difference is microseconds, and it makes the budget count from the
moment Docker's grace clock started — which is what it is actually racing.

## The signal ladder

```python
def _handle_shutdown_signal(signum, frame):
  if _exit_nudge_sent:
    # Our own nudge coming home. `default_int_handler` is typed `-> Never`; it
    # raises KeyboardInterrupt, which ends the handler. No `return` follows
    # because pyright (reportUnreachable) would reject it as dead code.
    signal.default_int_handler(signum, frame)

  elif not SHUTDOWN.is_set():
    _emit("shutdown underway; interrupt again to force")
    run_shutdown(ShutdownKind.GRACEFUL)

  else:
    match next(_confirm_counter):
      case 0:
        _emit("shutdown ALREADY underway; interrupt again to FORCE "
              "(drops non-critical teardown)")
      case 1:
        _emit("FORCING; dropping non-critical teardown")
        run_shutdown(ShutdownKind.FORCED)
      case _:
        _emit("hard interrupt")
        signal.default_int_handler(signum, frame)
```

**The first branch is `SHUTDOWN.is_set()`, not the counter.** This is what makes the reviewed
scenario behave: when a background `_handle_fatal` already tripped `FATAL`, the operator's *first*
keypress skips the "start a shutdown" rung and lands directly on the warning, because a shutdown
genuinely is already underway. Two presses to force, not three, and never a silent swallow.

**`_exit_nudge_sent` must be checked first**, before the `is_set()` branch, since `SHUTDOWN` is by
definition set when our nudge lands. An operator keypress racing the nudge is indistinguishable and
exits — benign, since that is what both parties wanted.

**`elif`/`else` rather than fall-through.** The current code at
[shutdown.py:443-454](../../src/aeth_ext/errors/shutdown.py#L443-L454) is correct only by accident
of `default_int_handler` raising; nothing structural says so, and a comment sits in the gap reading
as though the line below runs in both cases. Explicit `return` statements cannot be used — typeshed
types `default_int_handler` as `-> Never` ([signal.pyi:71](../../.venv/Lib/site-packages/pyright/dist/dist/typeshed-fallback/stdlib/signal.pyi#L71))
and the inherited pyright config sets `reportUnreachable = true`, so a trailing `return` is a type
error. Removing the fall-through gets the guarantee with no dead code.

**Rungs are monotonic and never reset.** No time-based decay: the whole ladder plays out inside
Docker's 10s window, and a decaying confirmation is a worse guard than a sticky one.

**A comment is owed at the hard-interrupt rung.** POSIX registers `SIGTERM` on the same handler
([shutdown.py:495](../../src/aeth_ext/errors/shutdown.py#L495)), so that rung calls
`default_int_handler` for a `SIGTERM`, raising `KeyboardInterrupt` for a signal whose real default
disposition is plain termination. That is deliberate — an exception unwind lets `atexit` still run
`logging.shutdown` and drain the `QueueListener`s, unlike `os._exit` — but the function name
misleads at that call site.

## Reporting policy

**All output the shutdown sequence generates itself goes to the raw diagnostic fd and never through
`logger`.** Three reasons, strongest first:

1. **Self-inflicted latency at the worst moment.** The interrupt pass arms write-through *before*
   any reporting happens, and write-through is a `write()` plus a `flush()` per entry
   ([history.py:318-319](../../src/aeth_ext/central_log_server/client/history.py#L318-L319)); the
   socket handler is the same over a network. Every progress line would pay a synchronous flush,
   against a 1–7 second budget, on a pipeline the sequence just deliberately made expensive.
2. **The transport is a registrant.** It runs last at `LOGGING_TRANSPORT_PRIORITY` because
   everything else needs logging while tearing down — but shutdown's own messages bracket the
   *whole* pass, so an end banner logged after the transport closed goes into a closed handler or
   nowhere. The raw fd is the only route that works across the entire sequence.
3. **Consistency.** The `INTERRUPT` phase docstring already forbids logging as a re-entrancy
   hazard. Extending that rule to the whole module beats a module where half the code may log about
   dismantling logging.

**Result:** all four existing `logger` calls — lines
[357](../../src/aeth_ext/errors/shutdown.py#L357),
[369](../../src/aeth_ext/errors/shutdown.py#L369),
[375](../../src/aeth_ext/errors/shutdown.py#L375) and
[426](../../src/aeth_ext/errors/shutdown.py#L426) — become `_emit`.

The module-level `logger` and `import logging` are **retained**, for exactly one caller: the single
`logger.critical` shutdown marker described in "The one log record" below. So the rule is not "this
module never logs" but the sharper and more checkable **shutdown.py makes exactly one logging call
in the lifetime of a process** — a property a reviewer can verify by grepping the module for
`logger.` and expecting a single hit.

**The boundary:** messages *shutdown.py generates* are raw-only, including tracebacks from failed
callbacks, since shutdown.py formats them. Messages *registered callbacks generate* are their own
business and unchanged; a callback logging post-arm pays the flush, but that is its existing
behavior and not this module's call.

### The single emit function

```python
_DIAG_FD = 2

def _emit(text: str) -> None:
  """Write a diagnostic line. Safe in interrupt context.

  `os.write` is a bare syscall holding no Python-level lock. `print` takes the
  buffered-writer lock and Rich takes its console lock -- either can be held by
  the code this handler interrupted, which deadlocks the process during the one
  sequence that must not hang.
  """
  try:
    os.write(_DIAG_FD, text.encode("utf-8", "replace"))
  except OSError:
    pass  # a closed or redirected fd must never break a shutdown
```

Python-level signal handlers run in the main thread's eval loop between bytecodes, so they may
allocate and call arbitrary Python. The hazard is never allocation — it is **non-reentrant locks
held by the interrupted code**. That is why `os.write` suffices and pre-encoded byte constants are
unnecessary.

**fd 2, not fd 1.** An interrupt-context write can land inside a partially flushed buffered stdout
write from Rich or the app, splicing our line into theirs; a different fd makes that structurally
impossible. It also keeps the deliberate duplicate distinguishable from the log line it mirrors,
since `console_rich.toml`/`console_plain.toml` put log output on stdout. Docker's log driver
captures both, tagged by stream, so nothing is lost in the deployment actually run.
[`web_viewer/__main__.py:20`](../../src/aeth_ext/central_log_server/web_viewer/__main__.py#L20)
already sets `Console(stderr=True)`.

### Emitted lines

Every existing `logger.*` call becomes an `_emit`, plus a start banner (kind, callback count) and an
end banner (elapsed, run/skipped counts). Severity moves into the text since there are no levels.

**`_emit` prepends the prefix itself**; callers pass bare message text. The prefix is
`[shutdown +N.NNs]` plus a trailing space, measured from `_t0`, which for triage beats a wall
clock — it is the number reasoned about against the budget. `_t0` is `None` until the first driver
sets it in `run_shutdown`, and the ladder's rung-0 line is emitted *before* that call, so `_emit`
renders a bare `[shutdown]` prefix when `_t0` is unset. That is the only line that can ever lack an
elapsed figure.

```text
[shutdown +0.00s] GRACEFUL requested; 6 threaded callbacks
[shutdown +0.01s] ARM FAILED: HistoryBuffer.arm_shutdown
[shutdown +1.24s] WARN budget exhausted; skipping HeartbeatMonitor.close
[shutdown +1.31s] teardown complete; 4 run, 1 skipped
```

**Arm failures are split across both contexts.** `_run_interrupt_pass` keeps collecting failures for
the threaded pass to report with full tracebacks, but also emits a bare one-line
`ARM FAILED: <label>` inline at the point of failure. If the process dies before the threaded pass
is scheduled, an arm failure is currently lost entirely — and an arm failure is precisely the
"durability may be compromised" signal most wanted during triage. One syscall, no locks, no
formatting.

## The one log record

A single `logger.critical` marker so log files carry the context that everything following it
happened during shutdown. **Placement is the whole question, and one of the two candidates is a
provable deadlock.**

**Rejected — inside `run_shutdown`, before the arm pass.** Tempting, because buffers are not yet in
write-through so the call is cheap. But `run_shutdown` is reachable from the signal handler.
`logging.Handler.lock` and `logging._lock` are `RLock`s, so same-thread re-entry does not deadlock —
which makes the path look survivable right up until
[`config/__init__.py:920`](../../src/aeth_ext/logging/config/__init__.py#L920):

```python
q = queue.Queue()  # unbounded
```

`queue.Queue.put` takes a plain non-reentrant `threading.Lock` via its `Condition`. A signal landing
while the main thread is inside `Queue.put` blocks on a lock that same thread already holds. The
process hangs forever, having run no teardown at all, and no further signal can help because the
handler that would process it is the thing wedged. This is the configured default for
`QueueHandler` in this repo, not a hypothetical.

**Chosen — first statement of `_run_threaded_pass`.** An ordinary thread, so every lock hazard
evaporates. Cost is exactly one write-through flush, and it is timeout-bounded: `HandshakeSocketHandler`
inherits stdlib `makeSocket`, which connects through `socket.create_connection(..., timeout=1.0)` and
leaves that timeout on the socket, so a `sendall` on the emit path cannot block indefinitely. The
ack-read helper at
[`client/__init__.py:86-102`](../../src/aeth_ext/central_log_server/client/__init__.py#L86-L102)
saves and restores the previous timeout around its own read, so that bound survives handshake reads.

The inversion that makes this actively right rather than merely acceptable: in write-through mode
the record is durable the instant it is written, whereas pre-arm it would land in a buffer that may
never flush. The marker meant to prove "everything below happened during shutdown" would otherwise
be the line most likely lost when the process dies. **The flush being paid for is precisely the
guarantee this line needs.**

Five properties make it safe:

1. `logger.critical` — no level configuration can filter out the one line whose job is always being
   present.
2. Exactly one, ever. `_run_threaded_pass` runs once per process under the `_drive_counter` guard,
   so the blast radius is bounded by construction rather than by discipline.
3. The raw fd-2 banner is emitted **first**, the log call second. If the log call stalls or dies the
   diagnostic is already out, and the elapsed prefix on the next raw line makes the stall visible
   and measurable.
4. Wrapped in `try/except BaseException`. `logging` already swallows handler errors; this also
   covers a formatting or filter bug, and nothing on a shutdown path may raise.
5. It is the first statement, ahead of arm-failure reporting and the callback loop, so it genuinely
   precedes everything it brackets.

**Residual risk, already handled.** A wedged transport could spend up to ~1s of budget on that line,
and `FATAL`'s entire budget is 1.0s. The budget machinery absorbs this with no new code: measuring
from `_t0` means a slow marker simply starts the skipping of non-`required` callbacks, while the
`required` transport arms/joins still run and the process still exits. The cost is paid in optional
teardown, which is exactly the trade the budget exists to make.

## Effect on the review finding

Defect A becomes unreachable — with `exit_when_done` gone, `_run_threaded_pass` always reaches
`_attempt_early_exit`, so `_exit_nudge_sent` is always eventually set. Defect B is closed by the
ladder: an operator signal arriving mid-shutdown hits the `else` branch and produces a visible
warning. **No signal is silently discarded on any rung.**

None of the reviewer's three proposed fixes is adopted. The ladder subsumes all of them and is
strictly better than their option (a), which would have hard-interrupted on the first keypress of a
shutdown the operator could not see.

## Testing

**Existing tests — the bulk of the mechanical work.** Every scenario in
[_optimized_scenarios.py](../../tests/errors/_optimized_scenarios.py) that reaches `_handle_fatal`
now receives an exit nudge; in a fresh subprocess with nothing registered it lands almost
immediately and races the rest of the function. Approximately 8 of the 15 qualify (the four
`alert_exception_*` and the three cancellation-propagation scenarios never trip a shutdown) — the
exact set is to be confirmed when writing the implementation plan. Each needs the
`try/except KeyboardInterrupt` wrapper that
[_optimized_scenarios.py:216-220](../../tests/errors/_optimized_scenarios.py#L216-L220) already
models, whose docstring rationale generalizes verbatim.
`report_exc_exit_when_done_alerts_and_requests_fatal_shutdown` is renamed — the behavior it tested
is now unconditional.

**New tests:**

- `ShutdownState` `FORCED` semantics, as pure unit tests on a fresh instance. The module docstring
  already prescribes building your own rather than tripping the global.
- Budget-zero skipping with a mixed `required`/non-`required` registry.
- The four ladder rungs, driven by `signal.raise_signal(SIGINT)` under `-O` subprocesses via the
  existing `_run_optimized` harness — required because the handler is `__debug__`-gated off.
- A regression test that our own nudge short-circuits past the ladder rather than landing on rung 0.
  This is the one genuinely subtle interaction in the design.
- Shutdown output assertions by capturing fd 2 in the `-O` subprocess, which is considerably easier
  to make deterministic than intercepting log records mid-teardown.

## Out of scope

The `emergency_fd()` handler protocol and the `HandshakeSocketHandler` priority slot — captured in
detail as [TODO.md](../../TODO.md) item 8. Deferred because they touch the handler base classes, the
dictConfig construction path, and the listener walk: a second feature inside a PR that still has an
open review finding.

Deferring is cheap because this design is forward-compatible with it. `_emit(text)` encodes and
writes to one fd today; it becomes a write to a list of fds. Nothing else moves.
