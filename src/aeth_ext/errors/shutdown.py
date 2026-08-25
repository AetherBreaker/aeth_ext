"""Process-wide shutdown signalling and the two-pass shutdown registry (D-I1/D-I2/D-I3).

Replaces the old bare ``aiologic.Event`` pair (``FATAL_EVENT``,
``SHUTDOWN_EVENT``) with one object carrying a *kind*, so consumers have a
single thing to watch and can tell which sort of shutdown is underway with one
read.

Shutdown runs in **two passes**:

- the **interrupt pass** runs inline wherever shutdown was triggered (typically
  a signal handler on the main thread), flipping participants into
  write-through mode -- this alone makes buffered data durable;
- the **threaded pass** runs on a thread spawned for the shutdown's duration,
  performing best-effort teardown against a time budget.

Durability comes entirely from the first pass, so it never depends on the
second one completing.

**This module reports on itself via raw writes to fd 2** (``_emit``), not
``logging`` -- see that function for why. The one exception is a single
``critical`` marker on the module logger at the top of the threaded pass, so
the log stream shows a shutdown happened. That gives a mechanically-checkable
invariant: grep this module for attribute access on the module logger and
expect exactly one hit.
"""

# Standard library imports
import atexit
import logging
import multiprocessing.connection as mp_connection
import os
import signal
from _thread import interrupt_main
from enum import IntEnum
from itertools import count
from threading import Event as ThreadingEvent, Lock, Thread, main_thread
from time import monotonic, sleep
from traceback import format_exception
from typing import TYPE_CHECKING, NamedTuple, override
from weakref import WeakMethod

# Third party imports
from aiologic import Event

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator
  from types import FrameType
  from typing import Any

  # First party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

  type ShutdownCallback = Callable[[tuple[ExceptionTrail, ...]], None]
  """A registered shutdown callback: one positional argument, every fatal
  ``ExceptionTrail`` accumulated so far this process (see
  ``register_for_shutdown``'s ``callback`` parameter). Empty when no fatal exception has
  occurred yet."""


logger = logging.getLogger(__name__)

__all__ = [
  "LOGGING_TRANSPORT_PRIORITY",
  "SHUTDOWN",
  "SHUTDOWN_COMPLETE",
  "SHUTDOWN_WAKEUP",
  "ShutdownCompletion",
  "ShutdownKind",
  "ShutdownPhase",
  "ShutdownState",
  "get_current_fatal_trails",
  "install_shutdown_signal_handlers",
  "register_for_shutdown",
  "run_shutdown",
]


class ShutdownKind(IntEnum):
  """How urgently the process is winding down.

  Ordered, and escalation only ever moves *up* this scale. The ordering is
  load-bearing: ``ShutdownState.request`` takes the maximum of the current
  and requested kinds, which is what makes concurrent requests order-independent.
  """

  RUNNING = 0
  """No shutdown has been requested."""

  GRACEFUL = 1
  """Wind down in an orderly way; there is a grace period to spend."""

  FATAL = 2
  """Something is broken. Spend as little time as possible -- what you would
  otherwise wait on may be the very thing that failed."""

  FORCED = 3
  """The operator has asked for the process to stop *now*. Drops every
  non-``required`` teardown callback immediately -- optional teardown is the
  cost of getting out this fast."""


_wakeup_read, _wakeup_write = mp_connection.Pipe(duplex=False)
"""Backing pair for SHUTDOWN_WAKEUP (D-I9). A real pipe on both platforms --
``multiprocessing.connection.Pipe()`` is POSIX ``os.pipe()``-backed there and a genuine
Windows named pipe (``PipeConnection``) here, both waitable through the single cross-platform
``multiprocessing.connection.wait()`` alongside a caller's own sockets. The write end is
private; only ``ShutdownState.request`` ever touches it.
"""


class ShutdownState:
  """One-shot, monotonically escalating shutdown signal.

  Waitable three ways: ``is_set()`` as a loop predicate, ``wait(timeout=...)``
  as a blocking sleep-or-wake, and ``await state`` on an event loop.

  **The kind is derived from one-shot sub-events, never a mutable field.**
  ``Event.set()`` is idempotent, thread-safe and irreversible, so monotonicity
  holds by construction with no lock needed -- not an optimization: the
  interrupt pass runs inside a signal handler on the main thread, where a lock
  the interrupted code already holds would deadlock.

  Reading checks ``FORCED``, then ``FATAL``, then ``GRACEFUL``, so a request
  racing a read can only observe the newer state, never a staler one.
  """

  __slots__ = ("_fatal", "_forced", "_graceful", "_requested")

  def __init__(self) -> None:
    self._graceful = Event()
    self._fatal = Event()
    self._forced = Event()
    # Set by all three kinds, giving waiters one thing to block on. Kept
    # separate from the others so e.g. a FATAL request wakes waiters without
    # implying a graceful request also happened.
    self._requested = Event()

  @property
  def kind(self) -> ShutdownKind:
    """The current kind, as a single atomic read.

    A property rather than an ``is_set()``-then-``.kind`` pair at the call
    site, since two separate reads could straddle an escalation.
    """
    if self._forced.is_set():
      return ShutdownKind.FORCED
    if self._fatal.is_set():
      return ShutdownKind.FATAL
    if self._graceful.is_set():
      return ShutdownKind.GRACEFUL
    return ShutdownKind.RUNNING

  def request(self, kind: ShutdownKind) -> None:
    """Request a shutdown of *kind*, escalating if one is already underway.

    Idempotent and safe to call concurrently from any thread, including from a
    signal handler. A request can never lower the current kind.
    """
    if kind is ShutdownKind.RUNNING:
      msg = "RUNNING is the absence of a shutdown request, not a request that can be made"
      raise ValueError(msg)

    first = not self._requested.is_set()

    # Publish the kind before the wake event, or a waiter released by
    # _requested could read a kind not yet published (RUNNING, mid-shutdown).
    if kind is ShutdownKind.FORCED:
      self._forced.set()
    elif kind is ShutdownKind.FATAL:
      self._fatal.set()
    else:
      self._graceful.set()
    self._requested.set()

    if first:
      # Wake anything waiting on SHUTDOWN_WAKEUP alongside its own sockets/pipes (D-I9).
      # Sent once, not on every escalation -- the pipe stays readable forever after this
      # one message, so a second write would only add an unread message nobody drains.
      # Reachable from a signal handler, so this must hold to ShutdownPhase.INTERRUPT's
      # constraints: non-blocking, lock-free. send_bytes() on a fresh, otherwise-idle pipe
      # can't realistically block; the OSError guard covers a closed/broken pipe, matching
      # this module's "shutdown's own signalling must never itself fail loudly" policy (see
      # _emit).
      #
      # The payload is one byte, not b"" -- confirmed empirically, not just by inspection:
      # on Windows 8+, multiprocessing.connection.wait()'s peek (a zero-length overlapped
      # ReadFile) treats a *zero-length* pending message as consumed by the act of checking
      # for it, so a second wait() with nothing having called recv_bytes() would come back
      # empty. A non-empty payload takes the normal pending-completion path instead, which
      # doesn't get consumed by checking -- SHUTDOWN_WAKEUP's whole contract (readable
      # forever, never drained) depends on this.
      try:
        _wakeup_write.send_bytes(b"\x00")
      except OSError:
        pass

  def is_set(self) -> bool:
    """Whether any shutdown has been requested. Use as a loop predicate."""
    return self._requested.is_set()

  def wait(self, timeout: float | None = None) -> bool:
    """Block until a shutdown is requested, or *timeout* elapses.

    Returns ``True`` if woken by a request and ``False`` if it timed out, so it
    can double as both a sleep and a stop signal in a polling loop.
    """
    return self._requested.wait(timeout=timeout)

  def __await__(self) -> Generator[Any, Any, bool]:
    """Await a shutdown request on an event loop.

    Also makes this usable with ``asyncio.wait_for``, as
    ``aeth_ext.monitoring.heartbeat`` does for its interval sleep.
    """
    return self._requested.__await__()

  @override
  def __repr__(self) -> str:
    return f"<{type(self).__name__} {self.kind.name}>"


SHUTDOWN = ShutdownState()
"""The process-wide shutdown signal.

One-shot and global, exactly like the ``FATAL_EVENT`` it replaces: once
requested it can never be cleared, so any test that trips it for real must run
in an isolated subprocess. Tests covering the *semantics* should build their own
``ShutdownState`` instead.

**Resolving means a shutdown was *requested*, not that teardown has run.** The
threaded pass has only just started when a waiter here wakes. Stop starting new
work and finish what you must, but do not return from your main coroutine (or
main thread) on this alone -- wait on ``SHUTDOWN_COMPLETE`` first, or stay
parked until the exit nudge unwinds you. A main that returns early is held at
interpreter exit until the pass finishes either way (``_join_pass_at_exit``);
waiting explicitly is what lets a tail run *after* it.
"""


class ShutdownCompletion:
  """One-shot signal that the threaded pass has finished (D-I4).

  Waitable the same three ways as ``ShutdownState``. Set after every threaded
  callback has run or been skipped, immediately before the exit nudge.

  Waiting is also a declaration, from any thread and even on a ``wait`` that
  times out: something has a tail to run once teardown is done, so
  ``_attempt_early_exit`` holds the nudge back for whatever remains of the
  shutdown budget (less a small margin), and never less than the short grace
  it gives an undeclared waiter. A tail that hangs is still unwound inside the
  budget; one that returns within its window is never interrupted, because the
  nudge is skipped once the main thread has finished. Poll ``is_set()`` for a
  check that declares nothing.
  """

  __slots__ = ("_done", "_waited")

  def __init__(self) -> None:
    self._done = Event()
    # Set by the first wait()/await, never cleared -- the "someone has a tail"
    # declaration read by _attempt_early_exit.
    self._waited = Event()

  def is_set(self) -> bool:
    """Whether the threaded pass has finished."""
    return self._done.is_set()

  def wait(self, timeout: float | None = None) -> bool:
    """Block until the threaded pass has finished, or *timeout* elapses."""
    self._waited.set()
    return self._done.wait(timeout=timeout)

  def __await__(self) -> Generator[Any, Any, bool]:
    """Await the end of the threaded pass on an event loop."""
    self._waited.set()
    return self._done.__await__()

  @override
  def __repr__(self) -> str:
    return f"<{type(self).__name__} {'done' if self.is_set() else 'pending'}>"


SHUTDOWN_COMPLETE = ShutdownCompletion()
"""Set once the threaded pass has finished -- every callback run or skipped.

The documented lifecycle for a main coroutine/thread whose exit condition is a
shutdown request::

    await SHUTDOWN            # requested: stop starting work, finish what you must
    ...
    await SHUTDOWN_COMPLETE   # teardown done: safe to run a short tail and return

One-shot and global like ``SHUTDOWN``, and only ever set by the threaded
pass -- i.e. by a *driven* shutdown (``run_shutdown``, the signal handlers,
``trigger_shutdown``, a fatal exception). A raw ``SHUTDOWN.request()`` starts
no pass and so never sets this; code requesting a shutdown must go through
``run_shutdown`` for the lifecycle above to hold.
"""

SHUTDOWN_WAKEUP = _wakeup_read
"""Readable the moment a shutdown is requested, for a thread doing its own blocking I/O
that needs to notice SHUTDOWN without spinning up a watcher thread of its own.

Pass it alongside your own sockets/pipes to ``multiprocessing.connection.wait()``, e.g.
``wait([my_socket, SHUTDOWN_WAKEUP], timeout=...)`` -- cross-platform with no branching,
since ``wait()`` already dispatches correctly per-platform for both.

**Never call ``recv_bytes()``/``recv()`` on it.** It is one shared connection for the whole
process; the single message written to it by ``ShutdownState.request`` is what keeps it
permanently "ready" for every caller checking it via ``wait()``. Draining it would silently
make it look un-ready to everyone else still watching it.
"""


class ShutdownPhase(IntEnum):
  """*Where* a shutdown callback runs.

  Independent of whether that callback promises anything -- see the
  ``required`` argument to ``register_for_shutdown``. A callback that must
  not be skipped may still belong on the thread, purely for ordering reasons.
  """

  INTERRUPT = 0
  """Runs inline wherever shutdown was triggered -- typically a signal handler
  on the main thread, between bytecodes.

  Callbacks in this phase **must** be non-blocking, **must not** acquire a lock
  the interrupted code might already hold, and **must not log** (a log call here
  re-enters the very system being armed). In exchange they are guaranteed to run
  even if the event loop is wedged, which is what makes them the right place to
  flip buffers into write-through mode.
  """

  THREADED = 1
  """Runs on a thread spawned for the shutdown's duration.

  May block, join threads, and wait on cross-loop futures. Subject to a time
  budget and therefore explicitly best-effort.
  """


LOGGING_TRANSPORT_PRIORITY = 1000
"""Priority for registrants that *are* the logging transport (D-I2).

Everything else depends on logging still working while it shuts down, so the
transport goes last. Downstream applications register at the default ``0``
and run first with no need to know this constant exists.

Exported so a consumer with logging-adjacent teardown of its own can place
itself relative to it.
"""

# Seconds allowed for the whole threaded pass. Docker's default grace period
# before SIGKILL is 10s, so the graceful budget must fit inside it with room
# for interpreter exit. Fatal gets almost none -- what we'd wait on may be the
# broken thing. Forced gets nothing: every non-required callback is skipped
# from the first iteration.
_GRACEFUL_BUDGET_SECS = 7.0
_FATAL_BUDGET_SECS = 1.0
_FORCED_BUDGET_SECS = 0.0

_BUDGETS = {
  ShutdownKind.GRACEFUL: _GRACEFUL_BUDGET_SECS,
  ShutdownKind.FATAL: _FATAL_BUDGET_SECS,
  ShutdownKind.FORCED: _FORCED_BUDGET_SECS,
}

# The exit nudge's timing (see _attempt_early_exit): how long it waits for a
# SHUTDOWN_COMPLETE waiter to arrive when none has yet, how much of the budget
# it leaves unspent when deferring for a waiter's tail, and how often both that
# deferral and an optional callback's bounded join re-check the budget.
_NUDGE_GRACE_SECS = 0.25
_NUDGE_MARGIN_SECS = 0.1
_TICK_SECS = 0.1


class _Registration(NamedTuple):
  """One registered callback plus the four independent knobs governing it."""

  get: Callable[[], ShutdownCallback | None]
  """Returns the callback, or ``None`` if its owner has been collected."""

  phase: ShutdownPhase
  priority: int
  required: bool
  label: str
  """Human-readable identity, captured at registration time so a failure can be
  attributed after the callback's owner may already be gone."""


# Copy-on-write: mutated only by rebinding to a new tuple under _registry_lock,
# read with a plain attribute load and no lock. That is what keeps the
# interrupt pass safe -- a signal mid-register_for_shutdown() would deadlock
# on any lock the reader shared with the writer.
_registrations: tuple[_Registration, ...] = ()
_registry_lock = Lock()

# next() on a count object is atomic under the GIL: lock-free test-and-set
# for "am I the first driver?", avoiding the interrupt-context lock hazard.
_drive_counter = count()

# Same lock-free idiom as _drive_counter, kept separate: this counts operator
# confirmations, not drivers -- conflating them would let a non-signal caller
# perturb the ladder's rung count.
_confirm_counter = count()

# Set by run_shutdown()'s first-mover branch, before the interrupt pass.
# Single origin for the elapsed-time prefix and the budget clock -- measured
# from here rather than after the (near-instant) interrupt pass so the budget
# counts from when Docker's grace period actually started.
_t0: float | None = None

# Set just before the threaded pass nudges the main thread via
# interrupt_main(), which simulates SIGINT and so re-enters our own handler --
# this lets that handler tell its own nudge apart from an operator's keypress.
_exit_nudge_sent = False

# Closes a race: run_shutdown() sets this just before returning, and the
# threaded pass waits on it before the early exit, so a fast pass can't
# interrupt the main thread while it's still inside Thread.start().
_drive_released = ThreadingEvent()

# The threaded pass's thread, once run_shutdown() has started it. Read by
# _join_pass_at_exit, so a main thread that returns before the pass finishes
# is held at interpreter exit until it does.
_pass_thread: Thread | None = None

# Set by the signal ladder's last rung, immediately before it raises
# KeyboardInterrupt on the main thread. Tells _join_pass_at_exit not to wait
# for the pass: the operator has asked for the process to die *despite* a
# wedged required callback, and joining it would turn four interrupts into a
# hang until SIGKILL. Never cleared: an application that catches that
# KeyboardInterrupt and carries on has, for the rest of the process, forfeited
# the exit-time join -- the operator asked for exactly that.
_hard_interrupted = False

_current_fatal_trails: tuple[ExceptionTrail, ...] = ()
"""Every fatal exception's `ExceptionTrail` seen so far, in arrival order. Appended to (never
mutated or overwritten) by `_handle_fatal` (`aeth_ext.errors.err_handling`) immediately before
each call to `run_shutdown(FATAL)`; never cleared, matching `SHUTDOWN`'s own one-shot semantics.

Copy-on-write, like `_registrations`: a concurrent second fatal exception rebinds this to a new
tuple under `_fatal_trail_lock` rather than clobbering the first trail (D-copilot-A). Both
shutdown passes re-read this global fresh for every callback invocation rather than snapshotting
it once, so a callback that hasn't run yet always sees every trail landed by the time it's called
-- only callbacks that already ran keep the (necessarily incomplete) view they were called with."""

_fatal_trail_lock = Lock()
"""Guards writes to `_current_fatal_trails` only. Distinct from `_registry_lock`: unlike that
lock, this one is never taken from interrupt-context code (`_set_current_fatal_trail` is only
ever called from `_handle_fatal`, which already logs and alerts before reaching it), so it carries
none of `_registry_lock`'s deadlock-avoidance constraints."""


def get_current_fatal_trails() -> tuple[ExceptionTrail, ...]:
  """Every fatal exception's `ExceptionTrail` seen so far this process, in arrival order.

  Returns:
    An empty tuple when no fatal exception has occurred yet (e.g. a signal-driven `GRACEFUL`
    shutdown, or `trigger_shutdown()` for a plain error condition with nothing to raise). Appended
    to by `_handle_fatal` (`aeth_ext.errors.err_handling`) immediately before each call to
    `run_shutdown(FATAL)` -- more than one entry means more than one fatal exception raced to
    trigger shutdown.

    This is the retrieval mechanism for cleanup code that runs behind the shutdown event (e.g.
    `await SHUTDOWN` in an application's main loop) rather than as a registered
    `register_for_shutdown` callback -- those receive the same tuple as a call argument instead.
  """
  return _current_fatal_trails


def _set_current_fatal_trail(trail: ExceptionTrail) -> None:
  """Append *trail* to `_current_fatal_trails`. Private, called only by
  `aeth_ext.errors.err_handling._handle_fatal`."""
  global _current_fatal_trails
  with _fatal_trail_lock:
    _current_fatal_trails = (*_current_fatal_trails, trail)


_DIAG_FD = 2
"""The file descriptor every diagnostic this module produces is written to.

Standard error, not standard output: an interrupt-context write can land
mid-flush inside a buffered stdout write from Rich or the app, splicing our
line into theirs -- a non-stdout descriptor makes that structurally
impossible. Caveat: under ``console_plain.toml``, log records also default to
``sys.stderr``, so the collision this avoids on stdout can still happen there;
accepted rather than designed away (see the ``emergency_fd()`` protocol,
deferred out of this work).
"""


def _emit(text: str) -> None:
  """Write one diagnostic line about the shutdown itself. Never raises.

  Raw ``os.write``, not ``logger``: a logged line would pay a synchronous
  flush against the shutdown's own budget, the logging transport may already
  be torn down, and ``ShutdownPhase.INTERRUPT`` forbids its own callbacks
  from logging anyway. ``os.write`` specifically because it holds no
  Python-level lock, unlike ``print``/Rich, which could deadlock against a
  lock the interrupted code already holds.

  Renders the ``[shutdown +N.NNs] <text>`` prefix here (elapsed from ``_t0``,
  the number the time budget is reasoned about in) so callers just pass bare
  text, trailing newline included or not.
  """
  prefix = "[shutdown] " if _t0 is None else f"[shutdown +{monotonic() - _t0:.2f}s] "
  try:
    os.write(_DIAG_FD, f"{prefix}{text.rstrip('\n')}\n".encode(errors="replace"))
  except OSError:
    # Deliberately silent. fd 2 may be closed or redirected to something that
    # has gone away, and a shutdown that cannot narrate itself must still run
    # to completion -- there is, by construction, nowhere left to report this.
    pass


def register_for_shutdown(
  callback: ShutdownCallback,
  *,
  phase: ShutdownPhase,
  priority: int = 0,
  required: bool = False,
) -> None:
  """Register *callback* to run during process shutdown (D-I2).

  Args:
    callback: The callback to run at shutdown. Takes exactly one positional argument: every
      fatal ``ExceptionTrail`` accumulated so far (see ``get_current_fatal_trails``), as an
      empty tuple if none has occurred yet.
    phase: *Where* it runs. See ``ShutdownPhase`` for the safety obligations
      each phase imposes -- they follow from the execution context and apply
      regardless of *required*.
    priority: Ascending within its phase; ties broken by registration order.
      ``aeth_ext``'s own registrants use a high priority to run **after** a
      downstream application's, since the logging transport must be torn
      down last -- anything torn down before it that then logs would write
      into a closed handler.
    required: Whether the threaded pass may skip this callback once its time
      budget is exhausted. Orthogonal to *phase*: a no-op for the interrupt
      pass, which has no budget. For the threaded pass it also decides *how*
      the callback runs: a required one runs inline and unbounded (a wedge
      holds process exit until SIGKILL); an optional one runs on a daemon
      worker joined against the remaining budget and is abandoned -- left
      running, no longer holding exit -- if it overruns. Abandonment frees
      the pass, not every lock the callback holds: one wedged inside e.g. a
      logging handler's lock still blocks ``atexit``'s ``logging.shutdown``.

  An object needing work in both phases simply registers twice.
  """
  global _registrations

  # Identity for the label: runtime class (not the qualname's defining class,
  # so inherited callbacks attribute to the actual registrant) plus the bare
  # method name. `__self__` is the class itself for a bound classmethod, so it
  # is only passed through `type()` when not already a class.
  owner = getattr(callback, "__self__", None)
  name = str(getattr(callback, "__qualname__", None) or repr(callback))
  if owner is None:
    label = name
  else:
    owner_type = owner if isinstance(owner, type) else type(owner)
    label = f"{owner_type.__name__}.{name.rpartition('.')[2]}"

  # Bound methods are held via WeakMethod so a registrant is collected once its
  # owner is dropped (a reconnecting client must not accumulate dead
  # registrations). A plain function/closure is held strongly instead, since a
  # weak reference to one would die immediately -- the registry is typically
  # its only referent.
  get = WeakMethod(callback) if owner is not None else (lambda: callback)

  entry = _Registration(
    get=get,
    phase=phase,
    priority=priority,
    required=required,
    label=label,
  )
  with _registry_lock:
    _registrations = (*_registrations, entry)


def _run_interrupt_pass() -> list[tuple[str, BaseException]]:
  """Flip every armed participant into write-through mode. Never raises.

  Failures are **returned as well as announced**. The full traceback belongs
  to the threaded pass, where formatting it costs nothing anybody's waiting
  on -- but that pass may never run, and if the process dies first the
  failure is lost. An arm failure is exactly the "durability may be
  compromised" signal triage wants most, worth a duplicate line to guarantee
  it escapes. The inline announcement is a bare label, no traceback: one
  syscall, no locks, no formatting -- what keeps it interrupt-safe.
  """
  failures: list[tuple[str, BaseException]] = []
  for reg in sorted((r for r in _registrations if r.phase is ShutdownPhase.INTERRUPT), key=lambda r: r.priority):
    callback = reg.get()
    if callback is None:
      continue
    try:
      callback(_current_fatal_trails)
    except BaseException as exc:  # noqa: BLE001 -- one bad arm must not block the rest
      failures.append((reg.label, exc))
      _emit(f"ARM FAILED: {reg.label}")
  return failures


def _call_teardown(label: str, callback: ShutdownCallback) -> None:
  """Invoke one threaded callback, reporting rather than propagating a failure.

  Shared by the inline (required) and worker-thread (optional) paths of
  ``_run_threaded_pass``.
  """
  try:
    callback(_current_fatal_trails)
  except BaseException as exc:  # noqa: BLE001 -- one bad teardown must not block the rest
    _emit(f"TEARDOWN FAILED: {label}\n{''.join(format_exception(exc))}")


def _run_threaded_pass(arm_failures: list[tuple[str, BaseException]]) -> None:
  """Best-effort teardown against a time budget. Never raises.

  Brackets itself with a start and end banner on ``_DIAG_FD``, so a reader
  can see how many callbacks the pass believed it had, what became of each,
  and how much of the budget it consumed. The end banner's ``run`` and
  ``skipped`` figures always sum to the start banner's count.
  """
  # Snapshotted before the banner so the two agree on what was counted.
  pending = sorted((r for r in _registrations if r.phase is ShutdownPhase.THREADED), key=lambda r: r.priority)
  _emit(f"{SHUTDOWN.kind.name} requested; {len(pending)} threaded callbacks")

  # The one logging call this module ever makes -- a marker in the log stream.
  # Here, not in run_shutdown() ahead of the arm pass: that call is reachable
  # from the signal handler, and the configured QueueHandler's put() takes a
  # non-reentrant Lock a same-thread signal could already hold, deadlocking
  # with no teardown run. This pass is an ordinary thread, so that's moot here.
  # `critical` so no level config filters it; after the raw banner above so
  # a stall here still leaves a diagnostic on record.
  try:
    logger.critical("Process shutdown underway: %s", SHUTDOWN.kind.name)
  except BaseException:  # noqa: BLE001, S110 -- nothing on a shutdown path may raise
    # logging already swallows handler errors; this also covers a formatting
    # or filter bug that would otherwise take teardown down with it.
    pass

  for label, exc in arm_failures:
    _emit(f"ARM FAILED: {label}\n{''.join(format_exception(exc))}")

  # _t0 is set by run_shutdown() before this thread starts; the fallback here
  # is defensive only, since this function has no other caller.
  started = _t0 if _t0 is not None else monotonic()
  run = 0
  skipped = 0
  for reg in pending:
    callback = reg.get()
    if callback is None:
      # Owner was collected between registration and here. Counted as
      # skipped, not ignored -- dropping it from both tallies would leave the
      # end banner unable to account for a callback the start banner promised.
      skipped += 1
      continue

    # Re-read the kind each iteration: escalating to FATAL/FORCED mid-pass
    # shrinks the remaining budget. >= matters: a zero (FORCED) budget must
    # skip the first non-required callback on this iteration, not wait for
    # the clock to tick past it.
    budget = _BUDGETS.get(SHUTDOWN.kind, _GRACEFUL_BUDGET_SECS)
    if monotonic() - started >= budget and not reg.required:
      skipped += 1
      _emit(f"WARN budget exhausted; skipping {reg.label}")
      continue

    # Counted before the call: a callback that raises or overruns still got
    # its turn, and filing it under skipped would claim teardown never
    # reached it.
    run += 1
    if reg.required:
      _call_teardown(reg.label, callback)
      continue

    # Optional callbacks are bounded: run on a daemon worker joined against
    # what remains of the budget (re-read each tick, so escalation shortens
    # it). One that overruns is abandoned -- it keeps running on its daemon
    # thread, which nothing joins -- instead of wedging this pass, which
    # _join_pass_at_exit *does* wait for, and so holding the process until
    # SIGKILL for work that was best-effort by declaration.
    worker = Thread(target=_call_teardown, args=(reg.label, callback), name=f"aeth-ext-teardown:{reg.label}", daemon=True)
    worker.start()
    while worker.is_alive():
      remaining = _BUDGETS.get(SHUTDOWN.kind, _GRACEFUL_BUDGET_SECS) - (monotonic() - started)
      if remaining <= 0:
        _emit(f"WARN budget exhausted; abandoning {reg.label}")
        break
      worker.join(timeout=min(remaining, _TICK_SECS))

  _emit(f"teardown complete; {run} run, {skipped} skipped")

  # Wait for run_shutdown() to return before hijacking the main thread. A pass
  # with few registrants finishes almost instantly, and without this the
  # interrupt could land while that thread is still inside Thread.start().
  # The timeout is a safety valve, not an expected path.
  _drive_released.wait(timeout=5.0)
  SHUTDOWN_COMPLETE._done.set()  # pyright: ignore[reportPrivateUsage]
  _attempt_early_exit()


def _attempt_early_exit() -> None:
  """Unwind the main thread so the interpreter exits normally (D-I4).

  Best-effort. Exits *normally* (never ``os._exit()``) so ``atexit`` still
  drains the ``QueueListener``s and runs ``logging.shutdown`` (D-I5) instead
  of idling out Docker's grace period until SIGKILL.

  Not ``signal.signal`` + ``interrupt_main`` directly: ``signal.signal`` only
  works from the main thread, and the signal handler can't just call
  ``signal.default_int_handler`` itself either, since that raises
  immediately and would kill this daemon thread mid-teardown. Instead
  ``_handle_shutdown_signal`` checks ``_exit_nudge_sent`` -- meaning only
  "our own nudge is in flight" -- and defers to the stock handler once set,
  which also covers non-signal triggers like a direct
  ``trigger_shutdown`` call.

  Some *other* non-daemon thread still blocking exit after this is not
  fought further: teardown finished and the data is durable, so SIGKILL is a
  correct outcome here.

  Three things hold the nudge back. Something that waited on
  ``SHUTDOWN_COMPLETE`` has a tail to run, so the nudge is deferred until the
  shutdown budget less ``_NUDGE_MARGIN_SECS`` is spent -- but never less than
  ``_NUDGE_GRACE_SECS`` after completion, since a pass whose required
  callbacks overran the budget would otherwise nudge the instant the waiter
  woke, racing the tail it was meant to protect. With no waiter yet, it waits
  that same grace (capped by the budget): a caller doing ``await SHUTDOWN``
  then ``await SHUTDOWN_COMPLETE`` reaches the second await one loop iteration
  after the first resolves, and a fast pass can finish inside that gap -- the
  grace lets the waiter arrive and be honoured (re-read each tick, as is the
  budget, so an escalation shortens either wait). And once the main thread has
  finished (its tail returned, or it never parked) there is nothing to unwind:
  the nudge is skipped rather than landing as a stray ``KeyboardInterrupt``
  inside the interpreter's own thread join. Best-effort: main can finish in
  the instant between that check and ``interrupt_main()``, in which case the
  interrupt lands in an atexit hook (reported as ignored) with no data impact.
  """
  global _exit_nudge_sent

  completed_at = monotonic()
  grace_deadline = completed_at + _NUDGE_GRACE_SECS
  while main_thread().is_alive():
    started = _t0 if _t0 is not None else completed_at
    budget_deadline = started + _BUDGETS.get(SHUTDOWN.kind, _GRACEFUL_BUDGET_SECS) - _NUDGE_MARGIN_SECS
    if SHUTDOWN_COMPLETE._waited.is_set():  # pyright: ignore[reportPrivateUsage]
      deadline = max(budget_deadline, grace_deadline)
    else:
      deadline = min(budget_deadline, grace_deadline)
    remaining = deadline - monotonic()
    if remaining <= 0:
      break
    sleep(min(remaining, _TICK_SECS))

  if not main_thread().is_alive():
    return

  _exit_nudge_sent = True
  try:
    interrupt_main()
  except Exception as exc:  # noqa: BLE001 -- the nudge is best-effort by design
    _emit(f"could not nudge the main thread to exit; leaving the process to SIGKILL\n{''.join(format_exception(exc))}")


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
  """Drive an escalating shutdown ladder in response to a caught OS signal (D-I3).

  Runs on the main thread between bytecodes. Four rungs, climbed one signal
  at a time, never reset (no decay -- the ladder plays out inside Docker's
  grace period before SIGKILL, where a sticky confirmation is the safer guard):

  1. **Start.** Nothing underway: runs the interrupt pass inline (durability,
     works even if the event loop is wedged), hands off teardown, returns.
  2. **Warn.** Shutdown already underway: warns one more forces it, and
     drives at whatever kind is already in force (no-op if a driver exists,
     rescue if not -- see that rung's comment).
  3. **Force.** Next signal escalates to ``ShutdownKind.FORCED``, dropping
     every non-``required`` callback from then on.
  4. **Hard interrupt.** Any further signal defers to Python's stock handler.

  Once teardown finishes, ``_attempt_early_exit`` arms this handler to raise
  instead -- rung 1's exit, checked ahead of the ladder.

  ``elif``/``else``, never a fall-through, so exactly one rung fires per
  signal; ``return`` can't do this job, since ``reportUnreachable = true``
  flags any code after ``signal.default_int_handler`` (typed ``-> Never``).
  """
  global _hard_interrupted

  if _exit_nudge_sent:
    # Our own interrupt_main() nudge coming home -- it simulates SIGINT, so it
    # re-enters this handler. Checked before SHUTDOWN.is_set() because that's
    # already true by the time a nudge is possible, which would otherwise send
    # our own nudge into a ladder rung instead of out here. An operator
    # keypress racing the nudge is indistinguishable from it and takes the
    # same exit either way -- benign, since that's what both parties wanted.
    #
    # Defers to Python's stock behaviour (raises KeyboardInterrupt) rather
    # than hand-rolling the raise, exiting the way a second Ctrl-C would.
    signal.default_int_handler(signum, frame)

  elif not SHUTDOWN.is_set():
    # Nothing underway. Warn before driving, not after -- by the time
    # run_shutdown() returns, the interrupt pass has run and the threaded
    # pass may be well underway, so a later warning would describe a
    # shutdown mid-flight instead of announcing it.
    _emit("shutdown underway; interrupt again to force")
    run_shutdown(ShutdownKind.GRACEFUL)

  else:
    # A shutdown is already underway, whether this operator started it or a
    # background _handle_fatal call did. Testing SHUTDOWN.is_set() rather
    # than the confirm counter is what makes the background-triggered case
    # behave: if FATAL tripped before any keypress, the operator's first
    # press lands here, not on the "start" branch -- two presses to force in
    # that case, not three, and no signal is silently discarded.
    match next(_confirm_counter):
      case 0:
        # First confirmation: warn what the next one does, don't escalate yet.
        _emit("shutdown ALREADY underway; interrupt again to FORCE (drops non-critical teardown)")

        # Drive anyway: a caller may set SHUTDOWN directly, bypassing
        # run_shutdown() (no in-tree code does any more, but the API allows
        # it), so is_set() can be true with no driver yet -- this rung must be
        # able to rescue that case, not just assume a driver exists. Costs
        # nothing when a driver already exists: run_shutdown() then no-ops.
        # kind is read fresh, never hardcoded, so a bypassing caller's kind is
        # honoured.
        run_shutdown(SHUTDOWN.kind)
      case 1:
        # Second confirmation: escalate to FORCED, dropping every
        # non-required callback in the threaded pass from here on.
        _emit("FORCING; dropping non-critical teardown")
        run_shutdown(ShutdownKind.FORCED)
      case _:
        # Third confirmation and beyond: FORCED only drops callbacks between
        # iterations of the threaded pass loop, so a wedged `required`
        # callback still needs a terminal answer. POSIX registers SIGTERM on
        # this same handler, so this rung is reachable by either signal --
        # default_int_handler's name suggests SIGINT-only, but the response
        # here is deliberately shared. It raises KeyboardInterrupt rather
        # than terminating outright (SIGTERM's real default), so the
        # exception unwind still lets atexit run logging.shutdown and drain
        # the QueueListeners, which a bare os._exit() would discard. The flag
        # keeps _join_pass_at_exit from re-blocking that unwind on the very
        # callback the operator is trying to escape.
        _emit("hard interrupt")
        _hard_interrupted = True
        signal.default_int_handler(signum, frame)


def install_shutdown_signal_handlers() -> None:
  """Install OS signal handlers that drive ``run_shutdown`` (D-I3/D-I4).

  **Not under a normal interpreter**: ``__debug__`` skips this, so dev
  Ctrl-C keeps Python's stock behaviour; production runs under ``python -O``.

  POSIX registers ``SIGINT``/``SIGTERM``; Windows registers ``SIGINT`` and,
  where available, ``SIGBREAK`` (Windows has no OS-level ``SIGTERM``).

  **Plain** ``signal.signal``, not ``loop.add_signal_handler()``: the asyncio
  version delivers via the event loop's FIFO ready queue with no preemption,
  so a wedged loop -- exactly the failure this system exists to survive --
  would never run it.
  """
  if __debug__:
    return

  # Standard library imports
  from sys import platform

  signal.signal(signal.SIGINT, _handle_shutdown_signal)
  if platform == "win32":
    if hasattr(signal, "SIGBREAK"):
      signal.signal(signal.SIGBREAK, _handle_shutdown_signal)
  else:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)


def run_shutdown(kind: ShutdownKind = ShutdownKind.GRACEFUL) -> None:
  """Request a shutdown of *kind* and drive both passes (D-I3).

  Safe to call from a signal handler, an ``except`` block, or any thread.
  Returns once the interrupt pass is done and the threaded pass handed off --
  never blocks the caller on teardown, so an event loop on the calling thread
  can keep running and loop-affine participants can still make progress.

  Only the first caller drives. A later call still *escalates* the kind
  (shrinking the in-flight budget) but doesn't start a second pass.
  """
  SHUTDOWN.request(kind)
  if next(_drive_counter) != 0:
    return

  global _pass_thread, _t0

  _t0 = monotonic()
  arm_failures = _run_interrupt_pass()
  # Daemon, with _join_pass_at_exit holding interpreter exit for it instead of
  # the interpreter's own non-daemon join. Both keep a main thread that returns
  # early (e.g. one whose exit condition was `await SHUTDOWN`) from letting the
  # process exit mid-callback, which would make `required=True` an empty
  # promise; only the atexit hook can also be told to stand down by the signal
  # ladder's last rung, which the interpreter's join cannot.
  _pass_thread = Thread(target=_run_threaded_pass, args=(arm_failures,), name="aeth-ext-shutdown", daemon=True)
  _pass_thread.start()
  # Registered here, not at import: atexit runs last-registered first, and the
  # logging QueueListener stop hooks are registered at logging-setup time --
  # after this module's import -- so an import-time hook would run *after*
  # them, with the listeners already stopped while the pass was still logging.
  # Registered now, it precedes everything registered before shutdown began.
  atexit.register(_join_pass_at_exit)
  _drive_released.set()


def _join_pass_at_exit() -> None:
  """Hold interpreter exit until the threaded pass has finished (D-I4).

  Registered with ``atexit`` by ``run_shutdown`` the moment the pass starts,
  which places it ahead of every hook registered earlier in the process --
  the logging ``QueueListener`` stops and ``logging.shutdown`` included -- so
  the pass, and the logging transport's own teardown within it, completes
  before logging is torn down under it. Reached only after
  ``threading._shutdown`` has marked the main thread done, so the pass's exit
  nudge sees ``main_thread().is_alive()`` false and stands down. A shutdown
  first driven from *inside* an atexit hook registers nothing (CPython drops
  hooks added during atexit processing), so that pass runs unjoined; nothing
  in this package does that.

  Unbounded, by design: a wedged ``required`` callback holds exit until Docker
  SIGKILLs the process. The one way out is the signal ladder's last rung,
  which sets ``_hard_interrupted`` so the operator's fourth interrupt unwinds
  the process instead of parking here.
  """
  if _pass_thread is not None and not _hard_interrupted:
    _pass_thread.join()
