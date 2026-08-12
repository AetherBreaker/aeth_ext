# Implementation plan — unconditional shutdown exit + confirm-then-force ladder

Derived from [shutdown-exit-and-force-ladder-design.md](shutdown-exit-and-force-ladder-design.md).
The design doc is the authority on *why*; this file is the authority on *what to build, in what order*.
Where they appear to conflict, the design doc governs and the discrepancy must be raised, not silently
resolved.

Branch: `feat/two-phase-logging-config`. Primary file: `src/aeth_ext/errors/shutdown.py`.

## Global Constraints

These bind every task. Violating any of them is a review defect regardless of what the task text says.

- **Never add `from __future__ import annotations`.** Explicit project rule (`.claude/CLAUDE.md`).
- **Indentation is 2 spaces**, matching every file in `src/aeth_ext/`. Docstrings are reStructuredText-ish
  prose in the existing house voice — long, explanatory, and stating *why*, not *what*.
- **PEP 758 is in effect** (Python 3.14): `except A, B, C:` without parens is valid unless `as e` is used.
- **Pyright runs with `reportUnreachable = true`.** `signal.default_int_handler` is typed `-> Never`,
  so **no `return` may follow a call to it** — use `elif`/`else` structure instead. Verify with
  `uv run pyright`.
- **Ruff and pyright must both pass** before a task reports DONE: `uv run ruff check .` and
  `uv run pyright`.
- **Testing cadence:** this is a feature branch. Run only targeted tests relevant to the task, once, at
  the end of the task. Do **not** run the full `uv run pytest` suite mid-task.
- **`PYTHONPYCACHEPREFIX`** must be set when running anything directly outside pytest.
- **Conventional Commits** (`<type>(<scope>): <summary>`); scope is `shutdown` for `shutdown.py` work,
  `errors` when `err_handling.py` is also touched. For `fix` commits the body must state (1) the bug,
  (2) the cause, (3) the fix.
- **Tests do not define intent.** An existing assertion is not evidence of desired behavior. If a change
  makes an assertion obsolete, rewrite or delete the test — never contort the implementation to keep a
  test green. You have standing authority to add, rewrite, or delete tests.
- **`SHUTDOWN` is a process-global one-shot.** Any test that trips it for real must run in an isolated
  subprocess (the existing `_run_optimized` harness). Tests covering *semantics* build their own
  `ShutdownState`.
- **Shutdown code runs in interrupt context.** No new locks, no `print`, no Rich, no `logger` calls on
  any path reachable from the signal handler, except the single sanctioned `logger.critical` in
  Task 3.

---

## Task 1 — `ShutdownKind.FORCED` and the three-way `ShutdownState`

**File:** `src/aeth_ext/errors/shutdown.py`, plus new unit tests.

Add a fourth kind and the state machinery behind it. This task is self-contained: nothing else in the
module changes behavior yet.

1. Add `FORCED = 3` to `ShutdownKind`, below `FATAL`, with a docstring in the existing voice explaining
   that it drops every non-`required` teardown callback immediately — the operator has asked for the
   process to stop now, and optional teardown is the cost.
2. Add `_FORCED_BUDGET_SECS = 0.0` alongside the existing budget constants and register it in
   `_BUDGETS`, giving:
   ```python
   _BUDGETS = {
     ShutdownKind.GRACEFUL: _GRACEFUL_BUDGET_SECS,
     ShutdownKind.FATAL: _FATAL_BUDGET_SECS,
     ShutdownKind.FORCED: _FORCED_BUDGET_SECS,
   }
   ```
3. `ShutdownState.__slots__` gains `_forced`; `__init__` creates a third one-shot `Event()`.
4. The `kind` property checks **`FORCED` → `FATAL` → `GRACEFUL` → `RUNNING`**, in that order. The
   existing docstring's guarantee — a read racing a request can only observe the *newer* state, never
   an older one — must be preserved and its wording updated to cover three kinds rather than two.
5. `request()` dispatches three ways. Keep the existing ordering guarantee comment: the kind sub-event
   is published **before** `self._requested.set()`, so a waiter released by `_requested` can never read
   `RUNNING`.
6. **Fix the budget comparison** at the `monotonic() - started > budget` line in `_run_threaded_pass`:
   it becomes `>=`. A zero budget must skip the *first* non-required callback rather than depending on
   the clock having ticked. This is the one behavior change outside the enum, and it is required for
   `FORCED` to mean anything.

**Do not** change `exit_when_done`, the signal handler, or any logging in this task.

### Tests for Task 1

New pure-unit tests on a **freshly constructed `ShutdownState`** (never the global `SHUTDOWN`), placed
alongside the existing shutdown tests — locate them first and follow their file/naming conventions.
Cover:

- A fresh state reads `RUNNING`.
- `request(FORCED)` on a fresh state reads `FORCED`, and `is_set()` is true.
- Escalation is monotonic: `GRACEFUL` then `FORCED` reads `FORCED`; `FORCED` then `GRACEFUL` still
  reads `FORCED`; `FATAL` then `FORCED` reads `FORCED`; `FORCED` then `FATAL` reads `FORCED`.
- `request(RUNNING)` still raises `ValueError`.
- `wait()` returns `True` after a `FORCED` request.
- `_BUDGETS[ShutdownKind.FORCED] == 0.0`.

---

## Task 2 — Delete `exit_when_done`; introduce `_t0` and `_exit_nudge_sent`

**Files:** `src/aeth_ext/errors/shutdown.py`, `src/aeth_ext/errors/err_handling.py`, and the existing
tests that break as a result.

The parameter is deleted, not defaulted — it has exactly one correct value. API breakage is a non-issue:
unmerged branch, no consumers.

### Production changes

1. **`run_shutdown`**: drop the `exit_when_done` keyword entirely. Signature becomes
   `def run_shutdown(kind: ShutdownKind = ShutdownKind.GRACEFUL) -> None:`. Delete the docstring block
   that existed only to justify the parameter (the paragraphs starting at *"`exit_when_done` asks the
   threaded pass..."* through the `handle_fatal_exc_*` paragraph). Keep the "only the first caller
   drives" paragraph.
2. **Set `_t0`** in `run_shutdown`, in the first-mover branch (after the `next(_drive_counter) != 0`
   early return, so only the driver sets it), **before** `_run_interrupt_pass()` is called. Declare it
   at module level as `_t0: float | None = None` with a comment explaining that it is the single origin
   for both the elapsed display prefix and the threaded pass's budget, and that measuring from before
   the interrupt pass is deliberate — that pass is atomic stores by construction, so the difference is
   microseconds, and it makes the budget count from the moment Docker's grace clock started.
3. **`_run_threaded_pass`**: drop the `exit_when_done` parameter and the `if not exit_when_done: return`
   guard. The function now **always** ends with `_drive_released.wait(timeout=5.0)` followed by
   `_attempt_early_exit()`. Replace its local `started = monotonic()` with the module-level `_t0`
   (defensively falling back to `monotonic()` if `_t0` is somehow `None`, since the function is only
   ever reached via `run_shutdown`, which sets it).
4. **Rename `_early_exit_armed` → `_exit_nudge_sent`** everywhere, and rewrite its module-level comment:
   the meaning narrows to *"our own `interrupt_main()` is in flight"*. It no longer doubles as an
   escape-hatch arming signal. Note in the comment why it cannot be deleted: `interrupt_main()`
   simulates SIGINT and so re-enters our own handler, which must be able to tell that apart from an
   operator keypress.
5. **`_attempt_early_exit`**: update the docstring — it no longer references `_early_exit_armed` by the
   old name, and the paragraph describing the handler consulting the flag must reflect the new
   narrowed meaning. Its behavior is otherwise unchanged.
6. **`err_handling.py`**: delete `exit_when_done` from `_handle_fatal`, `trigger_shutdown`, and
   `report_exc`, including their docstring paragraphs describing it. `_handle_fatal` calls
   `run_shutdown(ShutdownKind.FATAL)`; `trigger_shutdown` calls `run_shutdown(kind)`. **Keep
   `trigger_shutdown`'s `kind` parameter** — it still meaningfully selects the budget.
7. Grep the whole repo for `exit_when_done` afterwards and confirm zero hits outside
   `.claude/plans/` and `PLAN-retract-fallback-config.md` (historical docs; leave them alone).

**Do not** touch logging/reporting or the signal handler in this task.

### Test repairs for Task 2

Every scenario in `tests/errors/_optimized_scenarios.py` that reaches `_handle_fatal` or
`trigger_shutdown` now receives an exit nudge unconditionally. In a fresh subprocess with nothing
registered, the threaded pass finishes almost immediately and the resulting `KeyboardInterrupt` races
the rest of the scenario function.

- Audit **all** scenarios in that file and determine the exact set that now trips a shutdown. The
  design estimates ~8 of 15 qualify — the four `alert_exception_*` and the three
  cancellation-propagation scenarios do not. **Confirm this by reading, do not assume.**
- Each qualifying scenario needs the `try/except KeyboardInterrupt` wrapper already modeled at
  `_optimized_scenarios.py:216-220`; its docstring rationale generalizes verbatim, so reuse the
  reasoning rather than inventing new wording.
- Rename `report_exc_exit_when_done_alerts_and_requests_fatal_shutdown` — the behavior it tested is now
  unconditional. Pick a name describing what it now covers (e.g.
  `report_exc_alerts_and_requests_fatal_shutdown`), update the `_SCENARIOS` dict key, and update the
  corresponding test in `tests/errors/test_err_handling.py` (around line 231) including its docstring,
  which currently explains `exit_when_done=True` forwarding.

Run the targeted tests: `uv run pytest tests/errors/ -q`.

---

## Task 3 — Reporting policy: `_emit`, the banners, and the one log record

**File:** `src/aeth_ext/errors/shutdown.py`.

All output shutdown.py generates goes to raw fd 2 and never through `logger`, with exactly one
sanctioned exception. The invariant a reviewer can check by grep: **shutdown.py makes exactly one
logging call in the lifetime of a process.**

1. Add `_DIAG_FD = 2` and `_emit(text: str) -> None` at module level (`import os` at the top with the
   other stdlib imports). Implementation per the design:
   ```python
   def _emit(text: str) -> None:
     try:
       os.write(_DIAG_FD, text.encode("utf-8", "replace"))
     except OSError:
       pass
   ```
   with the design's docstring rationale: `os.write` is a bare syscall holding no Python-level lock,
   whereas `print` takes the buffered-writer lock and Rich takes its console lock — either can be held
   by the code the handler interrupted, deadlocking the process during the one sequence that must not
   hang. Also document **fd 2 rather than fd 1**: an interrupt-context write can splice into a
   partially flushed buffered stdout write from Rich or the app; a different fd makes that structurally
   impossible, and it keeps the deliberate duplicate distinguishable from the log line it mirrors.
2. **`_emit` prepends the prefix itself.** Callers pass bare message text with no prefix and no
   trailing newline; `_emit` renders `[shutdown +N.NNs] <text>\n`, elapsed measured from `_t0`. When
   `_t0` is `None` — which happens only for the ladder's rung-0 line, emitted before any
   `run_shutdown` call — render a bare `[shutdown] ` prefix. That is the only line that can ever lack
   an elapsed figure; say so in the docstring.
3. **Convert all four existing `logger.*` calls to `_emit`**, moving severity into the text since there
   are no levels:
   - the arm-failure loop in `_run_threaded_pass` (`logger.error(...)`) — full traceback text, since
     shutdown.py formats it (use `traceback.format_exception`);
   - the budget-exhausted `logger.warning` → `WARN budget exhausted; skipping <label>`;
   - the callback-failure `logger.exception` → the label plus formatted traceback;
   - the `logger.debug` in `_attempt_early_exit`.
4. **Add a start banner and an end banner** in `_run_threaded_pass`:
   - start: `<KIND> requested; <N> threaded callbacks`
   - end: `teardown complete; <R> run, <S> skipped` — so the loop must count runs and skips.
   Match the design's sample output shape:
   ```text
   [shutdown +0.00s] GRACEFUL requested; 6 threaded callbacks
   [shutdown +0.01s] ARM FAILED: HistoryBuffer.arm_shutdown
   [shutdown +1.24s] WARN budget exhausted; skipping HeartbeatMonitor.close
   [shutdown +1.31s] teardown complete; 4 run, 1 skipped
   ```
5. **Split arm-failure reporting across both contexts.** `_run_interrupt_pass` keeps collecting
   failures for the threaded pass to report with full tracebacks, **and additionally** emits a bare
   one-line `ARM FAILED: <label>` inline at the point of failure. Rationale for the docstring: if the
   process dies before the threaded pass is scheduled, an arm failure is currently lost entirely — and
   an arm failure is precisely the "durability may be compromised" signal most wanted during triage.
   One syscall, no locks, no formatting, so it is safe in interrupt context.
6. **The one log record.** As the **first statement** of `_run_threaded_pass` — ahead of the
   arm-failure reporting and the callback loop — emit the raw start banner **first**, then make a
   single `logger.critical` marker call, wrapped in `try/except BaseException: pass`. Order matters: if
   the log call stalls or dies, the diagnostic is already out and the elapsed prefix on the next raw
   line makes the stall visible and measurable. Add a comment covering the design's reasoning:
   - placing it inside `run_shutdown` before the arm pass is a **provable deadlock** —
     `run_shutdown` is reachable from the signal handler, and the repo's configured `QueueHandler`
     fronts an unbounded `queue.Queue` whose `put` takes a plain non-reentrant `threading.Lock`; a
     signal landing while the main thread is inside `Queue.put` hangs the process forever;
   - `_run_threaded_pass` is an ordinary thread, so every lock hazard evaporates;
   - it runs exactly once per process under the `_drive_counter` guard;
   - `critical` because no level configuration may filter out the one line whose job is always being
     present.
   Keep the module-level `logger` and `import logging` — this one caller is why they remain.
7. Confirm the invariant: grep `shutdown.py` for `logger.` and expect exactly one hit.

**Boundary:** messages that *registered callbacks* generate are their own business and unchanged. Only
messages shutdown.py itself generates become raw-only.

### Tests for Task 3

Deferred to Task 5, which owns fd-2 capture in `-O` subprocesses. This task must still leave the
existing targeted test suite green: `uv run pytest tests/errors/ -q`.

---

## Task 4 — The confirm-then-force signal ladder

**File:** `src/aeth_ext/errors/shutdown.py`.

Rewrite `_handle_shutdown_signal` as an explicit four-rung ladder. Structure exactly as designed:

```python
def _handle_shutdown_signal(signum, frame):
  if _exit_nudge_sent:
    signal.default_int_handler(signum, frame)

  elif not SHUTDOWN.is_set():
    _emit("shutdown underway; interrupt again to force")
    run_shutdown(ShutdownKind.GRACEFUL)

  else:
    match next(_confirm_counter):
      case 0:
        _emit("shutdown ALREADY underway; interrupt again to FORCE (drops non-critical teardown)")
      case 1:
        _emit("FORCING; dropping non-critical teardown")
        run_shutdown(ShutdownKind.FORCED)
      case _:
        _emit("hard interrupt")
        signal.default_int_handler(signum, frame)
```

1. Add `_confirm_counter = count()` at module level, with a comment explaining it is a per-signal ticket
   for the ladder using the same lock-free `next()`-under-GIL idiom as `_drive_counter`, for the same
   interrupt-context reason.
2. Keep `_drive_counter` unchanged — `run_shutdown` still has non-signal callers needing their own
   first-mover test.
3. **`elif`/`else`, never fall-through.** The current code is correct only by accident of
   `default_int_handler` raising; nothing structural says so. Explicit `return` statements are
   impossible (`-> Never` + `reportUnreachable`), so the branch structure is what provides the
   guarantee.
4. **`_exit_nudge_sent` is checked first**, before `is_set()`, since `SHUTDOWN` is by definition set
   when our own nudge lands. Document that an operator keypress racing the nudge is indistinguishable
   and exits — benign, since that is what both parties wanted.
5. **The second branch tests `SHUTDOWN.is_set()`, not the counter.** Document why: when a background
   `_handle_fatal` already tripped `FATAL`, the operator's *first* keypress skips the "start a
   shutdown" rung and lands directly on the warning, because a shutdown genuinely is already underway.
   Two presses to force, not three, and never a silent swallow.
6. **A comment is owed at the hard-interrupt rung.** POSIX registers `SIGTERM` on this same handler, so
   that rung calls `default_int_handler` for a `SIGTERM`, raising `KeyboardInterrupt` for a signal whose
   real default disposition is plain termination. That is deliberate — an exception unwind lets
   `atexit` still run `logging.shutdown` and drain the `QueueListener`s, unlike `os._exit` — but the
   function name misleads at that call site, so say so.
7. **Rungs are monotonic and never reset.** No time-based decay: the whole ladder plays out inside
   Docker's 10s window, and a decaying confirmation is a worse guard than a sticky one. Document it.
8. Update the `_handle_shutdown_signal` docstring: it currently describes the old two-branch behavior
   and promises a second Ctrl-C is a hard interrupt. Replace with a description of the four rungs.
9. **Leave the `__debug__` gate on `install_shutdown_signal_handlers` in place.** Handlers stay
   `-O`-only; dev Ctrl-C stays stock Python. The ladder is a guard on the force path, not a dev
   affordance — it justifies itself under `-O` alone.
10. Move the local `import signal` inside `_handle_shutdown_signal` to wherever it is cleanest given
    two call sites now need it; keep it module-local-vs-top-level consistent with how
    `install_shutdown_signal_handlers` does it, and do not introduce an import cost on a hot path.

Run targeted tests: `uv run pytest tests/errors/ -q`.

---

## Task 5 — New behavioral tests

**Files:** `tests/errors/` — extend the existing shutdown tests and `_optimized_scenarios.py`.

Read the existing `_run_optimized` harness and `_optimized_scenarios.py` conventions first; new
subprocess scenarios must follow them exactly (scenario function + `_SCENARIOS` dict entry + a test that
calls `_run_optimized`).

Cover, in this order:

1. **Budget-zero skipping.** With a mixed `required`/non-`required` registry, a `FORCED` shutdown must
   skip every non-`required` threaded callback from that moment while all `required` ones still run.
   Also cover the `>=` fix specifically: a zero budget skips the *first* non-required callback, not
   just later ones.
2. **The four ladder rungs**, driven by `signal.raise_signal(signal.SIGINT)` inside `-O` subprocesses
   via the existing `_run_optimized` harness. This is required because the handler is `__debug__`-gated
   off under a normal interpreter. Assert the rung sequence: first signal starts a graceful shutdown;
   second warns; third forces; fourth hard-interrupts.
3. **The nudge short-circuit regression.** Our own `interrupt_main()` must short-circuit past the ladder
   rather than landing on rung 0 — i.e. `_exit_nudge_sent` being checked first. The design calls this
   *the one genuinely subtle interaction*, so it gets its own dedicated test.
4. **Shutdown output assertions**, by capturing fd 2 in the `-O` subprocess. Assert the `[shutdown ...]`
   prefix shape, the start banner, the end banner with run/skipped counts, and that the rung-0 line
   carries the bare `[shutdown]` prefix (no elapsed figure) because `_t0` is unset at that point.
   Capturing fd 2 is considerably easier to make deterministic than intercepting log records
   mid-teardown, which is why the design chose it.

Finally, run the targeted suite once: `uv run pytest tests/errors/ -q`. If it is green, also run
`uv run pytest tests/central_log_server/test_client.py -q`, which references shutdown machinery.

---

## Out of scope

The `emergency_fd()` handler protocol and the `HandshakeSocketHandler` priority slot — captured as
[TODO.md](../../TODO.md) item 8. Do not implement them. The design is forward-compatible: `_emit`
becomes a write to a list of fds later, and nothing else moves.
