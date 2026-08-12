"""Process-wide shutdown signalling and the two-pass shutdown registry (D-I1/D-I2/D-I3).

Replaces the previous pair of bare ``aiologic.Event`` objects (``FATAL_EVENT``
and ``SHUTDOWN_EVENT``) with a single object carrying a *kind*, so consumers
have exactly one thing to watch and can tell -- with one read -- which sort of
shutdown is underway.

Shutdown runs in **two passes**:

- the **interrupt pass** runs inline wherever shutdown was triggered (typically
  a signal handler on the main thread) and does nothing but flip participants
  into write-through modes, which is what makes buffered data durable;
- the **threaded pass** runs on a thread spawned for the shutdown's duration and
  performs best-effort teardown against a time budget.

Durability is established entirely by the first pass, so it never depends on
the second one completing.

**Everything this module says about itself is written raw to fd 2**, via
:func:`_emit`, rather than through :mod:`logging` -- see that function for the
full reasoning. The one deliberate exception is a single ``critical`` marker
emitted on the module logger at the top of the threaded pass, so the log stream
carries the fact that a shutdown happened at all. That yields an invariant a
reviewer can check mechanically, by grepping this module for attribute access on
the module logger and expecting a single hit: **shutdown.py makes exactly one
logging call in the lifetime of a process.**
"""

# Standard library imports
import logging
import os
import weakref
from _thread import interrupt_main
from enum import IntEnum
from itertools import count
from threading import Event as ThreadingEvent, Lock, Thread
from time import monotonic
from traceback import format_exception
from typing import TYPE_CHECKING, NamedTuple, override

# Third party imports
from aiologic import Event

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator
  from types import FrameType
  from typing import Any


logger = logging.getLogger(__name__)

__all__ = [
  "LOGGING_TRANSPORT_PRIORITY",
  "SHUTDOWN",
  "ShutdownKind",
  "ShutdownPhase",
  "ShutdownState",
  "install_shutdown_signal_handlers",
  "register_for_shutdown",
  "run_shutdown",
]


class ShutdownKind(IntEnum):
  """How urgently the process is winding down.

  Ordered, and escalation only ever moves *up* this scale. The ordering is
  load-bearing: :meth:`ShutdownState.request` takes the maximum of the current
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


class ShutdownState:
  """One-shot, monotonically escalating shutdown signal.

  Waitable three ways, matching the three shapes already used across the
  codebase: ``state.is_set()`` as a loop predicate, ``state.wait(timeout=...)``
  as a blocking sleep-or-wake (its return value distinguishes the two), and
  ``await state`` on an event loop.

  **The kind is derived from one-shot sub-events, never stored in a mutable
  field.** ``Event.set()`` is idempotent, thread-safe and irreversible, so
  monotonicity holds by construction and there is no lost update to guard
  against -- which means *no lock*. That is not an optimization: the interrupt
  pass of :func:`~aeth_ext.errors.shutdown.run_shutdown` runs inside a signal
  handler on the main thread, where acquiring a lock the interrupted code might
  already hold would deadlock outright.

  Reading the kind checks ``FORCED`` first, then ``FATAL``, then ``GRACEFUL``,
  so a request racing with a read can only ever cause the *newer* state to be
  observed, never an older one.
  """

  __slots__ = ("_fatal", "_forced", "_graceful", "_requested")

  def __init__(self) -> None:
    self._graceful = Event()
    self._fatal = Event()
    self._forced = Event()
    # Set by *all three* kinds, so waiters have a single thing to block on.
    # Kept separate from the others rather than reusing one of them, because
    # e.g. a FATAL request must wake waiters without implying a graceful
    # request also happened.
    self._requested = Event()

  @property
  def kind(self) -> ShutdownKind:
    """The current kind, as a single atomic read.

    Deliberately a property rather than an ``is_set()``-then-``.kind`` pair at
    the call site: two separate reads can straddle an escalation and act on a
    kind that is already stale.
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

    # Publish the kind *before* the wake event. A waiter is released by
    # _requested, so setting that first would let it wake and read a kind that
    # has not been published yet -- observing RUNNING during a shutdown.
    if kind is ShutdownKind.FORCED:
      self._forced.set()
    elif kind is ShutdownKind.FATAL:
      self._fatal.set()
    else:
      self._graceful.set()
    self._requested.set()

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

    Also makes the object usable with :func:`asyncio.wait_for`, which is how
    :mod:`aeth_ext.monitoring.heartbeat` implements its interval sleep.
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
:class:`ShutdownState` instead.
"""


class ShutdownPhase(IntEnum):
  """*Where* a shutdown callback runs.

  Deliberately independent of whether that callback promises anything -- see
  the ``required`` argument to :func:`register_for_shutdown`. Registering for
  the interrupt phase is a statement about execution context and the safety
  rules that follow from it, **not** a promise that the callback delivers a
  result; and a callback that must not be skipped may still belong on the
  thread, purely for ordering reasons.
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

Every other registrant depends on logging still working while it shuts down, so
the transport has to go last. Downstream applications register at the default
``0`` and therefore run first, without having to know this constant exists or
coordinate with :mod:`aeth_ext` at all.

Exported so a consumer with its own logging-adjacent teardown can deliberately
place itself relative to it.
"""

# Seconds allowed for the whole threaded pass. Docker's default grace period
# before SIGKILL is 10s, so a graceful budget must fit comfortably inside it and
# still leave room for interpreter exit. A fatal shutdown gets almost none:
# something is broken, and what we would wait on may be the broken thing. A
# forced shutdown gets nothing at all: the operator has asked for the process
# to stop now, and every non-required callback is skipped from the first
# iteration onward.
_GRACEFUL_BUDGET_SECS = 7.0
_FATAL_BUDGET_SECS = 1.0
_FORCED_BUDGET_SECS = 0.0

_BUDGETS = {
  ShutdownKind.GRACEFUL: _GRACEFUL_BUDGET_SECS,
  ShutdownKind.FATAL: _FATAL_BUDGET_SECS,
  ShutdownKind.FORCED: _FORCED_BUDGET_SECS,
}


class _Registration(NamedTuple):
  """One registered callback plus the four independent knobs governing it."""

  get: Callable[[], Callable[[], None] | None]
  """Returns the callback, or ``None`` if its owner has been collected."""

  phase: ShutdownPhase
  priority: int
  required: bool
  label: str
  """Human-readable identity, captured at registration time so a failure can be
  attributed after the callback's owner may already be gone."""


# Copy-on-write: mutated only by rebinding to a new tuple under _registry_lock,
# and read with a plain attribute load and no lock at all. That is what makes
# the interrupt pass safe -- a signal landing while the main thread is midway
# through register_for_shutdown() would deadlock on any lock the reader shared
# with the writer.
_registrations: tuple[_Registration, ...] = ()
_registry_lock = Lock()

# next() on a C-implemented count object is atomic under the GIL, so this is a
# lock-free test-and-set for "am I the first caller to drive a shutdown?". A
# Lock here would reintroduce exactly the interrupt-context hazard the
# copy-on-write registry exists to avoid.
_drive_counter = count()

# Set once, by run_shutdown()'s first-mover branch, before the interrupt pass
# runs. The single origin for both the elapsed-time display prefix and the
# threaded pass's budget clock, replacing what used to be a local `started`.
# Measuring from before the interrupt pass rather than after it is deliberate:
# that pass is nothing but atomic stores by construction, so the difference is
# microseconds, and starting the clock this early makes the budget count from
# the moment Docker's grace period actually started.
_t0: float | None = None

# Set just before the threaded pass calls interrupt_main() to nudge the main
# thread, read by the signal handler on the main thread. Its meaning is
# narrow: "our own interrupt_main() call is in flight", nothing more -- it no
# longer doubles as an escape-hatch arming signal now that the nudge is
# unconditional. It cannot be deleted anyway: interrupt_main() simulates
# SIGINT, so it re-enters our own handler, which must be able to tell that
# apart from an operator's own keypress. A plain bool is enough: single
# writer, and the read is one atomic load.
_exit_nudge_sent = False

# Closes a race between the threaded pass and its own caller. run_shutdown()
# sets this immediately before returning; the threaded pass waits on it before
# attempting the early exit. Without it, a pass with few registrants can finish
# and interrupt the main thread while that thread is still inside
# Thread.start(), killing the process before run_shutdown() has even returned.
# Only the shutdown thread ever waits here, so the set() in interrupt context
# cannot contend with a lock the interrupted code already holds.
_drive_released = ThreadingEvent()

_DIAG_FD = 2
"""The file descriptor every diagnostic this module produces is written to.

Standard error rather than standard output. **The reason that carries the
choice on its own:** a write from interrupt context can land in the middle of a
partially flushed buffered *stdout* write from Rich or from the application
itself, splicing our line into theirs. Writing to a descriptor that is not
stdout makes that particular collision structurally impossible rather than
merely unlikely, and stdout is where the interactive rendering an operator is
watching actually goes.

There is a second, weaker property -- that the deliberate duplicate of the one
log record stays distinguishable from the record it mirrors -- and it is worth
being exact about, because **it does not hold under every shipped preset.**
Under ``console_rich.toml`` the handler renders to the injected
``runtime://console``, which is :func:`rich.get_console` and therefore stdout,
so there the two streams do separate cleanly. Under ``console_plain.toml`` they
do not: that preset configures a bare :class:`logging.StreamHandler` with no
``stream`` key, and a stream-less ``StreamHandler`` defaults to
:data:`sys.stderr`. Log records consequently share fd 2 with these lines, the
duplicate is not separable by stream, and an interrupt-context write here can in
principle splice into a partially flushed ``sys.stderr`` write from that
handler. Entry points that deliberately inject a stderr console -- the web
viewer does -- land in the same position under the Rich preset.

That residue is accepted rather than designed away: the primary rationale above
is about stdout and is unaffected by it, and the alternative (a dedicated
descriptor) is the ``emergency_fd()`` protocol deferred out of this work.
Docker's log driver captures both streams, tagged by stream, so nothing is lost
in the deployment actually run.
"""


def _emit(text: str) -> None:
  """Write one diagnostic line about the shutdown itself. Never raises.

  **This module reports through here and not through** ``logger``, for three
  reasons, strongest first.

  The interrupt pass arms write-through *before* any reporting happens, and
  write-through means a ``write()`` plus a ``flush()`` for every entry -- over a
  network, for the socket handler. Every progress line would therefore pay a
  synchronous flush, against a budget measured in single-digit seconds, on a
  pipeline the sequence just deliberately made expensive. The reporting would be
  competing with the teardown it is reporting on.

  The logging transport is itself a registrant, and runs last at
  :data:`LOGGING_TRANSPORT_PRIORITY` precisely because everything else needs
  logging while it tears itself down. But this module's own messages bracket the
  *whole* pass, so an end banner routed through ``logger`` would arrive after the
  transport had closed and go into a dead handler or nowhere at all. The raw
  descriptor is the only route that works across the entire sequence.

  And consistency: :attr:`ShutdownPhase.INTERRUPT` already forbids its callbacks
  from logging as a re-entrancy hazard, so a module that logged about dismantling
  logging would be holding its registrants to a rule it broke itself.

  :func:`os.write` in particular is a bare syscall holding no Python-level lock.
  That is what makes this safe in interrupt context: Python-level signal handlers
  run in the main thread's eval loop between bytecodes, so they may allocate and
  call arbitrary Python freely -- the hazard is never allocation, it is a
  **non-reentrant lock already held by the interrupted code**. ``print`` takes the
  buffered writer's lock and Rich takes its console lock, and either can be held
  by whatever the handler interrupted, which would deadlock the process during the
  one sequence that must not hang. For the same reason pre-encoded byte constants
  would buy nothing, so callers pass ordinary strings.

  **The prefix is rendered here, not by callers**, who pass bare message text with
  no prefix and no trailing newline. Lines read ``[shutdown +N.NNs] <text>``, with
  the elapsed figure measured from :data:`_t0` -- for triage that beats a wall
  clock, because it is the same number the time budget is reasoned about in.
  :data:`_t0` is unset until the first driver reaches :func:`run_shutdown`, and a
  line emitted before then renders a bare ``[shutdown]`` prefix instead. Only the
  signal ladder's first rung, which speaks before it decides to drive a shutdown
  at all, can ever lack an elapsed figure.
  """
  prefix = "[shutdown] " if _t0 is None else f"[shutdown +{monotonic() - _t0:.2f}s] "
  try:
    os.write(_DIAG_FD, f"{prefix}{text}\n".encode(errors="replace"))
  except OSError:
    # Deliberately silent. fd 2 may be closed or redirected to something that
    # has gone away, and a shutdown that cannot narrate itself must still run
    # to completion -- there is, by construction, nowhere left to report this.
    pass


def _format_exc(exc: BaseException) -> str:
  """Render *exc* as traceback text, ready to append to an :func:`_emit` line.

  This module formats its own tracebacks now that it no longer hands exceptions
  to ``logger`` for that. The trailing newline
  :func:`traceback.format_exception` leaves in place is stripped because
  :func:`_emit` supplies the line terminator itself; leaving it would put a
  blank line after every traceback.
  """
  return "".join(format_exception(exc)).rstrip("\n")


def _describe(callback: Callable[[], None]) -> str:
  """Human-readable identity for *callback*, captured at registration time.

  For a bound method this is the **runtime** class plus the bare method name.
  ``__qualname__`` already carries the method's *defining* class, so it is
  trimmed to its last segment before the runtime type is prefixed -- prefixing
  the whole qualname would render every label as
  ``HistoryBuffer.HistoryBuffer.arm_shutdown``. Using the qualname alone instead
  would be wrong in the other direction: an inherited callback would be
  attributed to the base class that happens to define it rather than to the
  object that actually registered it, which is the one a reader needs to find.

  ``__self__`` is the *class* for a bound classmethod rather than an instance,
  so the owner is only passed through :func:`type` when it is not already a
  class. Without that check the prefix would come out as the metaclass name and
  a classmethod registrant would label as ``type.arm_shutdown`` -- the one shape
  where trimming the qualname would otherwise lose the class name outright
  instead of merely repeating it. ``WeakMethod`` accepts a class-bound method,
  so this is a registration that can really arrive.
  """
  owner = getattr(callback, "__self__", None)
  name = str(getattr(callback, "__qualname__", None) or repr(callback))
  if owner is None:
    return name
  owner_type = owner if isinstance(owner, type) else type(owner)
  return f"{owner_type.__name__}.{name.rpartition('.')[2]}"


def _weak_getter(callback: Callable[[], None]) -> Callable[[], Callable[[], None] | None]:
  """Return a zero-arg getter for *callback* that does not keep its owner alive.

  Bound methods are held via :class:`weakref.WeakMethod`, so a registrant is
  collected normally once its owner is dropped -- a reconnecting client that
  rebuilds its drainers must not accumulate registrations for dead ones. A plain
  function or closure is held **strongly**, because a weak reference to one
  would die immediately: the registry is typically its only referent.
  """
  if hasattr(callback, "__self__"):
    # WeakMethod is itself a zero-arg callable returning the bound method, or
    # None once its owner is collected -- exactly this getter's contract.
    return weakref.WeakMethod(callback)
  return lambda: callback


def register_for_shutdown(
  callback: Callable[[], None],
  *,
  phase: ShutdownPhase,
  priority: int = 0,
  required: bool = False,
) -> None:
  """Register *callback* to run during process shutdown (D-I2).

  :param phase: *Where* it runs. See :class:`ShutdownPhase` for the safety
    obligations each phase imposes -- they follow from the execution context and
    apply regardless of *required*.
  :param priority: Ascending within its phase; ties broken by registration
    order. ``aeth_ext``'s own registrants use a high priority so they run
    **after** a downstream application's, because the logging transport must be
    torn down last -- anything shut down before it that then logs would write
    into a closed handler.
  :param required: Whether the threaded pass may skip this callback once its
    time budget is exhausted. Purely a skip policy, and orthogonal to *phase*:
    the interrupt pass has no budget, so this is a no-op there.

  An object needing work in both phases simply registers twice.
  """
  global _registrations

  entry = _Registration(
    get=_weak_getter(callback),
    phase=phase,
    priority=priority,
    required=required,
    label=_describe(callback),
  )
  with _registry_lock:
    _registrations = (*_registrations, entry)


def _ordered(phase: ShutdownPhase) -> list[_Registration]:
  """Live registrations for *phase*, in run order.

  ``sorted`` is stable, so equal priorities keep registration order. Reads the
  module global once -- copy-on-write means that single load is a consistent
  snapshot with no lock.
  """
  snapshot = _registrations
  return sorted((r for r in snapshot if r.phase is phase), key=lambda r: r.priority)


def _run_interrupt_pass() -> list[tuple[str, BaseException]]:
  """Flip every armed participant into write-through mode. Never raises.

  Failures are **returned as well as announced**, and the split is deliberate.
  The full traceback belongs to the threaded pass, where formatting one costs
  nothing anybody is waiting on -- but that pass may never be scheduled, and if
  the process dies first the failure is lost entirely. An arm failure is
  precisely the "durability may be compromised" signal triage wants most, so it
  is worth a duplicate line to guarantee it escapes. The inline announcement is
  a bare label with no traceback: one syscall, no locks, no formatting, which is
  what keeps it safe to do from interrupt context.
  """
  failures: list[tuple[str, BaseException]] = []
  for reg in _ordered(ShutdownPhase.INTERRUPT):
    callback = reg.get()
    if callback is None:
      continue
    try:
      callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad arm must not block the rest
      failures.append((reg.label, exc))
      _emit(f"ARM FAILED: {reg.label}")
  return failures


def _run_threaded_pass(arm_failures: list[tuple[str, BaseException]]) -> None:
  """Best-effort teardown against a time budget. Never raises.

  Brackets itself with a start and an end banner on :data:`_DIAG_FD`, so a
  reader can see how many callbacks the pass believed it had, what became of
  each, and how much of the budget the whole thing consumed. The end banner's
  ``run`` and ``skipped`` figures always sum to the count in the start banner,
  which is what makes the pair readable together.
  """
  # Snapshotted before the banner so the two agree: the banner promises a count,
  # and the loop below must be iterating over exactly what was counted.
  pending = _ordered(ShutdownPhase.THREADED)
  _emit(f"{SHUTDOWN.kind.name} requested; {len(pending)} threaded callbacks")

  # The one logging call this module makes, ever. Its job is to leave a marker
  # in the log stream itself, so a reader of the logs knows that everything
  # around it happened during a shutdown.
  #
  # Here, and nowhere else. Putting it in run_shutdown() ahead of the arm pass
  # is tempting -- buffers are not in write-through yet, so it would be cheap --
  # and it is a provable deadlock. run_shutdown() is reachable from the signal
  # handler, and while logging's own locks are RLocks that tolerate same-thread
  # re-entry, this repo's configured QueueHandler fronts an unbounded
  # queue.Queue, whose put() takes a plain non-reentrant threading.Lock via its
  # Condition. A signal landing while the main thread sits inside that put()
  # blocks on a lock the same thread already holds: the process hangs forever
  # having run no teardown at all, and no further signal can rescue it, because
  # the handler that would process one is the thing that is wedged. This pass is
  # an ordinary thread, so every one of those hazards evaporates.
  #
  # critical, because no level configuration may filter out the one line whose
  # whole job is being present. Exactly once per process, since _drive_counter
  # admits only one driver. And *after* the raw banner above, deliberately: if
  # this call stalls or dies the diagnostic is already out, and the elapsed
  # prefix on the next raw line makes the stall visible and measurable.
  try:
    logger.critical("Process shutdown underway: %s", SHUTDOWN.kind.name)
  except BaseException:  # noqa: BLE001, S110 -- nothing on a shutdown path may raise
    # logging already swallows handler errors; this also covers a formatting or
    # filter bug, which would otherwise take the entire teardown down with it.
    pass

  for label, exc in arm_failures:
    _emit(f"ARM FAILED: {label}\n{_format_exc(exc)}")

  # _t0 is set by run_shutdown() before this thread is even started; the
  # fallback below is defensive only -- this function has no other caller.
  started = _t0 if _t0 is not None else monotonic()
  run = 0
  skipped = 0
  for reg in pending:
    callback = reg.get()
    if callback is None:
      # The owner was collected between registration and here. Counted as
      # skipped rather than ignored: nothing ran, and dropping it from both
      # tallies would leave the end banner unable to account for a callback the
      # start banner had already promised.
      skipped += 1
      continue

    # Re-read the kind each iteration: an escalation to FATAL or FORCED
    # mid-pass shrinks the budget for everything still to come. >= rather than
    # > matters here: a zero (FORCED) budget must skip the *first* non-required
    # callback on this very iteration, not wait for the clock to tick past it.
    budget = _BUDGETS.get(SHUTDOWN.kind, _GRACEFUL_BUDGET_SECS)
    if monotonic() - started >= budget and not reg.required:
      skipped += 1
      _emit(f"WARN budget exhausted; skipping {reg.label}")
      continue

    # Counted before the call, not after it. A callback that raises did get its
    # turn, and the line below says what happened to it; filing it under
    # skipped would claim teardown never reached it, which is the opposite of
    # the truth and the wrong place to send someone reading the banner.
    run += 1
    try:
      callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad teardown must not block the rest
      _emit(f"TEARDOWN FAILED: {reg.label}\n{_format_exc(exc)}")

  _emit(f"teardown complete; {run} run, {skipped} skipped")

  # Wait for run_shutdown() to return to its caller before hijacking the main
  # thread. A pass with few registrants finishes almost instantly, and without
  # this the interrupt can land while that thread is still inside
  # Thread.start(). The timeout is a safety valve, not an expected path.
  _drive_released.wait(timeout=5.0)
  _attempt_early_exit()


def _attempt_early_exit() -> None:
  """Unwind the main thread so the interpreter exits normally (D-I4).

  Best-effort, like the rest of the threaded pass. When it works the process
  stops as soon as teardown is done instead of idling until Docker's grace
  period expires and SIGKILL lands -- and, crucially, it exits *normally*, so
  ``atexit`` still runs. That is what drains the ``QueueListener``s and closes
  every handler via ``logging.shutdown``, which is deliberately left on
  ``atexit`` rather than moved into this registry (D-I5).

  ``os._exit()`` is never used: it would skip ``atexit`` entirely and discard
  exactly the teardown this is trying to reach.

  Mechanism, and why it is not simply ``signal.signal`` + ``interrupt_main``:
  :func:`signal.signal` may only be called from the main thread, so this pass
  cannot restore the default ``SIGINT`` handler before nudging it. Nor can the
  signal handler simply defer to :func:`signal.default_int_handler` on its way
  out -- that would raise *immediately*, before any teardown has run, and since
  this pass is a daemon thread, main unwinding to interpreter exit would kill it
  mid-flight. Exiting only once teardown is done is the whole point.

  Instead :func:`_handle_shutdown_signal` consults :data:`_exit_nudge_sent` --
  which marks only that our own :func:`interrupt_main` call is in flight, not
  any kind of readiness signal -- and defers to the stock handler once it is
  set, so ``_thread.interrupt_main()`` unwinds main whether our handler is
  installed or the stock one is -- which also covers triggers that never
  involved a signal at all, such as
  :func:`~aeth_ext.errors.err_handling.trigger_shutdown` being called directly
  for a rejected remote logging config.

  If a non-daemon thread then blocks interpreter exit anyway, that is not fought
  further: our handlers finished and the data is durable, so letting SIGKILL
  arrive is the correct outcome rather than a failure.
  """
  global _exit_nudge_sent

  _exit_nudge_sent = True
  try:
    interrupt_main()
  except Exception as exc:  # noqa: BLE001 -- the nudge is best-effort by design
    _emit(f"could not nudge the main thread to exit; leaving the process to SIGKILL\n{_format_exc(exc)}")


def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
  """Drive a graceful shutdown in response to a caught OS signal (D-I3).

  Runs on the main thread between bytecodes. It performs the interrupt pass
  inline -- that is the whole point, since that pass is what makes buffered
  records durable and it must run even if the event loop is wedged -- then hands
  teardown to the shutdown thread and returns promptly, leaving whatever was
  running to continue.

  Once teardown has finished, :func:`_attempt_early_exit` arms this handler to
  raise instead, so the nudge it sends unwinds the main thread. The same branch
  makes a *second* Ctrl-C a hard interrupt, which is the conventional and
  expected escape hatch when a shutdown is taking too long.
  """
  if _exit_nudge_sent:
    # Standard library imports
    import signal

    # Hand straight back to Python's stock behaviour (which raises
    # KeyboardInterrupt) rather than hand-rolling the raise, so a second Ctrl-C
    # does exactly what a user pressing it expects.
    signal.default_int_handler(signum, frame)

  # This handler swallowed the signal that would otherwise have terminated the
  # process, so nothing else is going to unwind it -- the shutdown thread's
  # exit nudge, once teardown is done, is what does.
  run_shutdown(ShutdownKind.GRACEFUL)


def install_shutdown_signal_handlers() -> None:
  """Install OS signal handlers that drive :func:`run_shutdown` (D-I3/D-I4).

  **Not installed under a normal interpreter.** Graceful shutdown exists for
  production; during development, waiting one out on every Ctrl-C would just
  waste time, so ``__debug__`` leaves Python's stock behaviour completely
  untouched. Production runs under ``python -O``, matching every other
  ``__debug__``-gated behaviour in :mod:`aeth_ext.errors`.

  Platform dispatch mirrors the ``platform in (...)`` branch used for the event
  loop policy choice. POSIX registers ``SIGINT``/``SIGTERM``. Windows registers
  ``SIGINT`` and, where available, ``SIGBREAK`` -- Windows has no OS-level
  ``SIGTERM`` delivery mechanism, so nothing is lost by not registering it
  there. This stays within the standard-library :mod:`signal` module; no
  ``pywin32``/console-control-handler dependency is introduced.

  **Plain** :func:`signal.signal` **rather than** ``loop.add_signal_handler()``,
  deliberately. The asyncio version delivers its callback via a self-pipe into
  the event loop's plain FIFO ready queue -- no priority, no preemption. A loop
  stuck in a long synchronous block runs it late; a deadlocked loop never runs
  it at all. That is exactly the failure this system exists to survive (a
  wedged loop is a reason to shut down, not an assumption this can lean on).
  ``winloop.Loop.add_signal_handler()`` does work correctly on Windows, unlike
  stock asyncio -- rejected on the priority/preemption grounds above, not for
  lack of platform support.
  """
  if __debug__:
    return

  # Standard library imports
  import signal
  from sys import platform

  signal.signal(signal.SIGINT, _handle_shutdown_signal)
  if platform == "win32":
    if hasattr(signal, "SIGBREAK"):
      signal.signal(signal.SIGBREAK, _handle_shutdown_signal)
  else:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)


def run_shutdown(kind: ShutdownKind = ShutdownKind.GRACEFUL) -> None:
  """Request a shutdown of *kind* and drive both passes (D-I3).

  Safe to call from a signal handler, from an ``except`` block, and from any
  thread. Returns as soon as the interrupt pass is done and the threaded pass
  has been handed to its own thread -- it never blocks the caller on teardown,
  which is what lets an event loop on the calling thread keep running so
  loop-affine participants can still make progress.

  Only the first caller drives. A later call still *escalates* the kind (and so
  shrinks the in-flight budget), but does not start a second pass.
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
