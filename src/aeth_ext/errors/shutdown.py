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
import inspect
import logging
import multiprocessing.connection as mp_connection
import os
import signal
from _thread import interrupt_main
from enum import IntEnum
from itertools import count
from threading import Event as ThreadingEvent, Lock, Thread
from time import monotonic
from traceback import format_exception
from typing import TYPE_CHECKING, NamedTuple, override
from weakref import WeakMethod

# Third party imports
from aiologic import Event

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator
  from types import FrameType
  from typing import Any, TypeIs

  # First party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

  type ShutdownCallback = Callable[[], None] | Callable[[ExceptionTrail | None], None]
  """A registered shutdown callback: zero-arg, or one-arg accepting the current
  ``ExceptionTrail | None`` (see ``register_for_shutdown``'s ``callback`` parameter)."""


logger = logging.getLogger(__name__)

__all__ = [
  "LOGGING_TRANSPORT_PRIORITY",
  "SHUTDOWN",
  "SHUTDOWN_WAKEUP",
  "ShutdownKind",
  "ShutdownPhase",
  "ShutdownState",
  "get_current_fatal_trail",
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


class _Registration(NamedTuple):
  """One registered callback plus the five independent knobs governing it."""

  get: Callable[[], ShutdownCallback | None]
  """Returns the callback, or ``None`` if its owner has been collected."""

  phase: ShutdownPhase
  priority: int
  required: bool
  wants_trail: bool
  """Whether this callback accepts the current fatal trail as its one positional argument."""

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

_current_fatal_trail: ExceptionTrail | None = None
"""Set by `_handle_fatal` (`aeth_ext.errors.err_handling`) immediately before it calls
`run_shutdown(FATAL)`; never cleared afterward, matching `SHUTDOWN`'s own one-shot semantics."""


def get_current_fatal_trail() -> ExceptionTrail | None:
  """The `ExceptionTrail` for the exception currently driving a fatal shutdown, if any.

  Returns:
    `None` when no shutdown is underway, or the current shutdown was not triggered by a live
    exception (e.g. a signal-driven `GRACEFUL` shutdown, or `trigger_shutdown()` for a plain error
    condition with nothing to raise). Set by `_handle_fatal` (`aeth_ext.errors.err_handling`)
    immediately before it calls `run_shutdown(FATAL)`.

    This is the retrieval mechanism for cleanup code that runs behind the shutdown event (e.g.
    `await SHUTDOWN` in an application's main loop) rather than as a registered
    `register_for_shutdown` callback -- those receive the trail as a call argument instead.
  """
  return _current_fatal_trail


def _set_current_fatal_trail(trail: ExceptionTrail) -> None:
  """Private setter, called only by `aeth_ext.errors.err_handling._handle_fatal`."""
  global _current_fatal_trail
  _current_fatal_trail = trail


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
    callback: The callback to run at shutdown -- either zero-arg, or accepting exactly one
      positional argument (the current ``ExceptionTrail | None``, from
      :func:`get_current_fatal_trail`). Arity is detected once via `inspect.signature` at
      registration time.
    phase: *Where* it runs. See ``ShutdownPhase`` for the safety obligations
      each phase imposes -- they follow from the execution context and apply
      regardless of *required*.
    priority: Ascending within its phase; ties broken by registration order.
      ``aeth_ext``'s own registrants use a high priority to run **after** a
      downstream application's, since the logging transport must be torn
      down last -- anything torn down before it that then logs would write
      into a closed handler.
    required: Whether the threaded pass may skip this callback once its time
      budget is exhausted. A skip policy only, orthogonal to *phase*: a
      no-op for the interrupt pass, which has no budget.

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

  try:
    wants_trail = len(inspect.signature(callback).parameters) == 1
  except TypeError, ValueError:
    wants_trail = False

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
    wants_trail=wants_trail,
    label=label,
  )
  with _registry_lock:
    _registrations = (*_registrations, entry)


def _accepts_trail(callback: ShutdownCallback) -> TypeIs[Callable[[ExceptionTrail | None], None]]:
  """Type-checker-only narrowing for a resolved ``ShutdownCallback``, mirroring the same
  arity check ``register_for_shutdown`` uses to compute ``wants_trail``. Wrapped in an
  ``assert`` at each call site rather than called directly, so it (and this call) vanish
  under ``python -O`` -- production pays nothing for a fact ``wants_trail`` already records.
  """
  return len(inspect.signature(callback).parameters) == 1


def _no_trail(callback: ShutdownCallback) -> TypeIs[Callable[[], None]]:
  """The zero-arg counterpart to ``_accepts_trail``. A ``TypeIs`` narrowing a union of two
  ``Callable`` shapes can't be inverted by Pyright the way a plain class union can (arity
  isn't a subtractable type), so the zero-arg branch needs its own positive check rather
  than ``assert not _accepts_trail(...)``."""
  return len(inspect.signature(callback).parameters) == 0


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
      if reg.wants_trail:
        assert _accepts_trail(callback)
        callback(_current_fatal_trail)
      else:
        assert _no_trail(callback)
        callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad arm must not block the rest
      failures.append((reg.label, exc))
      _emit(f"ARM FAILED: {reg.label}")
  return failures


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

    # Counted before the call: a callback that raises still got its turn, and
    # filing it under skipped would claim teardown never reached it.
    run += 1
    try:
      # See _accepts_trail/_no_trail for why these asserts, not a plain call, are needed.
      if reg.wants_trail:
        assert _accepts_trail(callback)
        callback(_current_fatal_trail)
      else:
        assert _no_trail(callback)
        callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad teardown must not block the rest
      _emit(f"TEARDOWN FAILED: {reg.label}\n{''.join(format_exception(exc))}")

  _emit(f"teardown complete; {run} run, {skipped} skipped")

  # Wait for run_shutdown() to return before hijacking the main thread. A pass
  # with few registrants finishes almost instantly, and without this the
  # interrupt could land while that thread is still inside Thread.start().
  # The timeout is a safety valve, not an expected path.
  _drive_released.wait(timeout=5.0)
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

  A non-daemon thread blocking exit anyway is not fought further: teardown
  finished and the data is durable, so SIGKILL is a correct outcome here.
  """
  global _exit_nudge_sent

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

        # Drive anyway: some callers set SHUTDOWN directly, bypassing
        # run_shutdown() (e.g. central_log_server's _watch_for_shutdown_signal),
        # so is_set() can be true with no driver yet -- this rung must be able
        # to rescue that case, not just assume a driver exists. Costs nothing
        # when a driver already exists: run_shutdown() then no-ops. kind is
        # read fresh, never hardcoded, so a bypassing caller's kind is honoured.
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
        # the QueueListeners, which a bare os._exit() would discard.
        _emit("hard interrupt")
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
  global _t0

  SHUTDOWN.request(kind)
  if next(_drive_counter) != 0:
    return

  _t0 = monotonic()
  arm_failures = _run_interrupt_pass()
  Thread(
    target=_run_threaded_pass,
    args=(arm_failures,),
    name="aeth-ext-shutdown",
    daemon=True,
  ).start()
  _drive_released.set()
