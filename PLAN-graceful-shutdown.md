# Graceful Shutdown (D-I series)

**Branch:** `feat/two-phase-logging-config` — supersedes steps 10–14 / decisions D-H1–D-H7 of
[PLAN-two-phase-logging-config.md](PLAN-two-phase-logging-config.md)

> **Status:** all architectural decisions below are **settled**. Each carries its rationale so it
> does not need re-deriving in a later session without this context.

---

## Context

Steps 1–9 of the two-phase logging config plan are complete. Steps 10–14 (the D-H graceful-shutdown
registry) remain. Reviewing that design against the codebase turned up defects that make it
unimplementable as written:

1. **Nothing terminates.** `_handle_shutdown_signal` ([__init__.py:23](src/aeth_ext/__init__.py#L23))
   replaces Python's default SIGINT handler and only sets an event, so Ctrl-C is swallowed and
   SIGTERM under Docker waits out the grace period and gets SIGKILLed. Two non-daemon threads
   guarantee the hang.
2. **`SHUTDOWN_EVENT` has zero readers.** Every real shutdown consumer waits on `FATAL_EVENT`
   (`startup.main`, `LogWriterThread._record_loop`, `reader_server`, both heartbeat loops). The
   registry was being bolted to the event nothing listens to.
3. **D-H3's ordering is inverted.** It runs `aeth_ext`'s registrants first, but those *are* the
   logging transport every other registrant depends on — a downstream registrant that logs during
   its own shutdown would write into an already-closed handler.
4. **D-H1 addressed signal-safety, not re-entrancy.** Its arm callback calls `flush()` in interrupt
   context, re-entering `_flush_to_disk()`'s `popleft()` drain loop on the same thread.
5. **D-H7's premise is wrong.** `EmergencyHistoryWriter`'s `queue.Queue` *is* in-memory buffering,
   and its thread is `daemon=True`, so interpreter exit abandons whatever is still queued.
6. **The registry and the events were unsynchronized.** Callbacks returned while event-driven
   consumers were still winding down and still emitting records, so "the registry finished" said
   nothing about whether it was safe to tear down the transport.

The organizing idea of the replacement: **shutdown runs in two passes — an interrupt-context pass
and a threaded pass — and durability is established by arming write-through modes in the first pass,
so it never depends on teardown completing.**

## Guarantee

Every record is durable in the local JSONL history. Remote delivery to the central log server during
shutdown is best-effort and may be lost. Teardown tidiness is best-effort.

---

## Settled decisions

### D-I1 — One `ShutdownState` object replaces `FATAL_EVENT` and `SHUTDOWN_EVENT`

Lives in [err_handling.py](src/aeth_ext/errors/err_handling.py). Composes `aiologic.Event` rather
than reimplementing it — the codebase relies on `await ev`, `ev.wait(timeout=...)` **and its return
value**, and `.is_set()`.

State is **derived from one-shot sub-events, never stored in a mutable field**:

```python
def _current(self) -> ShutdownKind:
  if self._fatal.is_set():    return ShutdownKind.FATAL
  if self._graceful.is_set(): return ShutdownKind.GRACEFUL
  return ShutdownKind.RUNNING
```

`Event.set()` is idempotent, thread-safe, and irreversible, so monotonic escalation
(RUNNING → GRACEFUL → FATAL, never backwards) holds **by construction with no lock**. That matters
directly: the interrupt pass runs in signal context on the main thread, and a `threading.Lock`
guarding a mutable state field would be a live self-deadlock hazard. Concurrent setters both
succeed — there is no lost update regardless of arrival order. Checking FATAL first means a racing
escalation can only ever be observed as *newer*, never older.

**Consumers do not branch on state.** The difference between graceful and fatal is a *budget*, not a
different sequence: graceful means there is a grace period to spend on joins and thorough flushes;
fatal means something is broken and possibly the broken thing is what you would be waiting on. Only
the threaded pass reads the state, and it reads it to pick a timeout. Escalation shortens the
remaining budget; it never pivots anyone to a second code path. This is why "what can a consumer do
if it escalates mid-sequence?" has a satisfying answer — nothing, and nothing is required of it.
Stale reads fail safe: acting on a stale `GRACEFUL` means taking too long, never too little.

Replacing the two exported events is a breaking change for downstream consumers, consistent with the
existing D-E5 doctrine that all consumers update together.

### D-I2 — Registration: execution context and guarantee strength are orthogonal

**These two axes must not be bundled.** A best-effort handler may still prefer interrupt context; a
handler that must not be skipped may still belong on the thread for ordering reasons. Registering
for interrupt context must not imply promising a result. One registration is one callable with four
independent properties:

```python
def register_for_shutdown(
  callback: Callable[[], None],
  *,
  phase: ShutdownPhase,     # INTERRUPT | THREADED - *where* it runs
  priority: int = 0,        # ascending, within its phase
  required: bool = False,   # may the budget skip it?
) -> None: ...
```

An object needing work in both passes registers twice. This replaces D-H2's single
`SupportsGracefulShutdown` Protocol, whose two-method shape was precisely what married the axes.

**`phase` dictates safety obligations, not promises.** Anything registered `INTERRUPT` runs on the
main thread between bytecodes and must therefore be non-blocking, must not acquire a lock it could
already hold, and must be **silent** — a log call there re-enters the very system being armed. These
constraints follow from the execution context and apply regardless of `required`.

**`required` dictates skip policy only.** A required callback is never skipped because the budget is
exhausted, and its failure is logged at higher severity. The interrupt pass has no budget, so
`required` is a no-op there — kept for orthogonality rather than pretending it does something.

Registry is **copy-on-write**: append under a lock, readers take the immutable tuple reference with
no lock at all. A signal landing mid-registration therefore cannot self-deadlock. Hold callbacks
weakly — note that a bound method needs `weakref.WeakMethod`, since a plain `weakref.ref(obj.method)`
dies immediately, and that `__slots__` classes need `__weakref__` to be registrable at all (relevant
here given the project's heavy use of `IsPydanticSlots`).

Ordering: single global priority ascending within each phase, ties by registration order.
`aeth_ext`'s own registrants use a high priority so they land **after** downstream app registrants —
the logging transport must tear down last. D-H3's two-tier package grouping and its
`sys._getframe` package auto-detection are both dropped.

### D-I3 — The two passes

`run_shutdown()` is one function invoked in two contexts. (Earlier drafts called this "the driver";
it is not a component, just the code that walks the registry.)

**Interrupt pass** — signal handler, main thread, signal context: set the state, run every
`INTERRUPT` callback inline in priority order, spawn the threaded pass, return. Arming callbacks are
flag flips, so the whole pass costs microseconds and is safe mid-bytecode. **This is where the
durability guarantee is established.**

**Threaded pass** — a thread spawned for the shutdown's duration only: walks `THREADED` callbacks in
priority order against a budget, re-reading state between callbacks and skipping non-`required` ones
once exhausted. A thread is used because it is the only participant guaranteed schedulable
regardless of the event loop's state — which is the entire point, since a wedged loop is a reason to
shut down. This satisfies D-H1's actual intent (no threads living for a significant portion of the
program's lifetime) rather than its literal wording.

**`loop.add_signal_handler()` is explicitly rejected.** Its callback is delivered by writing to a
self-pipe and scheduling via `call_soon` into the loop's **FIFO ready queue** — no priority, no
preemption. A loop stuck in a long synchronous block runs it late; a deadlocked loop never runs it at
all. That is exactly the failure this must survive: the `QueueForwardHandler.green_put` event-loop
deadlock that started this whole investigation. `run_coroutine_threadsafe(...).result(timeout)` from
the threaded pass is strictly better because it can actually time out.

*(Verified during design: `winloop.Loop.add_signal_handler()` does work on Windows, unlike stock
asyncio. It was rejected on the priority grounds above, not on platform support.)*

**`__debug__` guards.** The signal handler is **not installed at all** under a normal interpreter, so
development keeps default Ctrl-C behaviour and nobody waits out a graceful shutdown while testing.
The `if __debug__: return` guards are **removed** from the registry and from
`handle_config_rejected`'s shutdown path, so the machinery itself is exercised in dev and by tests.

### D-I4 — Best-effort early exit, then let SIGKILL do its job

When the threaded pass finishes, attempt a normal interpreter exit so `atexit` runs and the container
stops early rather than burning the full grace period waiting for death with nothing left to do.
This is **best-effort like everything else.**

- Mechanism (**revised during implementation** — the sequence originally written here cannot work):
  `signal.signal()` may only be called from the main thread, and the threaded pass runs on its own,
  so it cannot restore the default `SIGINT` handler before nudging. Nor can the signal handler simply
  defer to `signal.default_int_handler` on its way out: that raises *immediately*, before any teardown
  has run, and since the pass is a daemon thread, main unwinding to interpreter exit would kill it
  mid-flight — exiting only *after* teardown is the entire point. And `default_int_handler` cannot be
  called from the pass itself either, because exceptions do not cross thread boundaries: it would
  raise on the daemon thread and leave main untouched.

  What actually works: the pass sets an armed flag and calls `_thread.interrupt_main()`, which is the
  only stdlib mechanism that can raise *in the main thread* from another thread (it sets CPython's
  SIGINT trip flag). Our handler, running on main, sees the flag and defers to
  `signal.default_int_handler` there. This also covers triggers that never involved a signal at all,
  such as `handle_config_rejected`, and makes a second Ctrl-C a hard interrupt.

- **`exit_when_done` is opt-in per trigger** (also added during implementation). Firing the exit at the
  end of every threaded pass broke `handle_fatal_exc_*`/`report_exc`, whose contract is to swallow and
  return `None`: a pass with no registrants completes instantly, so `interrupt_main()` landed while the
  triggering thread was still inside `Thread.start()`, killing the process with `0xC000013A`. Only the
  signal handler and `handle_config_rejected` request the exit — the paths where nothing else will
  unwind the process. A release gate additionally prevents the pass from ever outrunning its own
  caller.
- Interpreter shutdown joins every non-daemon thread before running `atexit`, and both
  `LogWriterThread` ([writer_thread.py:129](src/aeth_ext/central_log_server/server/writer_thread.py#L129))
  and `ThreadedQueueDrainer._thread` ([client/__init__.py:890](src/aeth_ext/central_log_server/client/__init__.py#L890))
  are non-daemon. Registering them (D-I8) means they normally stop in time. **If one hangs the exit
  anyway, do not fight it** — no `os._exit()`, no forced join, no daemonizing. Our handlers completed
  and the data is durable; SIGKILL is then doing exactly what it exists for.
- Never `os._exit()`: it skips `atexit`, losing the `QueueListener` drain and `logging.shutdown()`.
- Budget expiry within the pass: skip non-`required` callbacks and continue.

### D-I5 — `atexit` for logging teardown stays as-is

`logging.shutdown` is registered via `atexit` at logging-module import time; `atexit` runs LIFO, so
the `QueueListener.stop` calls registered later at
[setup.py:283,300](src/aeth_ext/logging/setup.py#L283) already run **before** handlers close.
Drain-then-close is already the correct sequence, and interpreter exit is naturally after every
registry callback. Moving the listeners into the registry would *break* this by hoisting them ahead
of every other registrant. **No change to these call sites.**

The original objection — "`atexit` doesn't run on SIGTERM" — is true only of today's signal handler,
which swallows the signal. Once D-I4's early exit lands, the process unwinds to interpreter exit
normally and `atexit` runs.

### D-I6 — `RecordHistoryBuffer` write-through mode

- Arm callback (INTERRUPT, required): flip `self._shutting_down = True`. **Nothing else** — no flush.
  Flushing in signal context would re-enter `_flush_to_disk()`'s drain loop on the same thread.
- `append()` when armed: write through instead of the threshold-gated `_maybe_flush()`. Because
  `_flush_to_disk()` drains the whole deque, the first post-arm `append()` also catches up the
  existing backlog.
- Teardown callback (THREADED): catch-up flush, covering buffers that receive no further records.
- **Add a lock around the write path.** The class currently has none, and `append()` is already
  called from arbitrary threads by all three client classes. Write-through turns a rare race into a
  per-record one: concurrent `open("a")` + write to the same path interleaves partial JSON lines into
  the *middle* of the file, which D-I7's repair cannot fix (it only inspects the trailing segment).
  Windows append mode is not atomic, and log lines routinely exceed the size at which POSIX
  `O_APPEND` is. Consider a held-open handle, as `EmergencyHistoryWriter` already does.

### D-I7 — Startup repair of a truncated trailing record

As D-H6, with one fix: **seek to the end and read a bounded tail** rather than reading the whole file
to inspect its last byte. Current day's file only. If it does not end in `b"\n"`, parse the suspect
trailing segment: unparseable means a genuine partial write, so truncate at the last complete `\n`
(log the byte length, not the content — a partial write may not be valid UTF-8); parseable means a
complete record missing only its newline, so append the `\n` rather than deleting a valid record.
Wrap both branches in `try/except OSError` — this is best-effort hygiene, not a construction
dependency.

### D-I8 — Registrants

| Participant | Registrations |
|---|---|
| `HandshakeSocketHandler` | arm history + checkpoint — INTERRUPT, required · `close()` — THREADED |
| `AsyncioQueueDrainer` | arm history + checkpoint — INTERRUPT, required · `run_coroutine_threadsafe(aclose(), loop).result(t)` — THREADED |
| `ThreadedQueueDrainer` | arm history + checkpoint — INTERRUPT, required · `stop(timeout)` — THREADED |
| `EmergencyHistoryWriter` | *(none — see note below)* |
| `ThreadedIdCheckpointBackend` | write-through — INTERRUPT, required · `close()` — THREADED |
| `AsyncioIdCheckpointBackend` | write-through — INTERRUPT, required · `close()` — THREADED — **currently `pass`; a real hole that needs implementing** |
| `LogWriterThread` | bounded `join()` — THREADED, required |
| `startup.main` | `tcp_server.close()`, `heartbeat_task.cancel()` — INTERRUPT |

Two entries changed during implementation:

- **`EmergencyHistoryWriter` is not registered separately.** It is created and destroyed dynamically
  as emergency mode is entered and left, and every owner's teardown already calls its `close()`
  (sentinel + join) — so it is covered transitively by the owners-register decision above. A separate
  registration would hold a weak reference to an object that is routinely replaced, and would add a
  second, redundant close path for no gain.
- **`startup.main` gets no threaded registration.** What remained for it was `runner.cleanup()`, which
  is loop-affine and therefore unreachable in exactly the wedged-loop case such a registration would
  exist for, and `writer.join()`, which `LogWriterThread` now registers for itself. When the loop is
  alive, `main`'s own `finally` performs both — a registration would only risk cleaning up twice.

**Owners register, not `RecordHistoryBuffer` itself.** The three client classes already sequence
checkpoint, emergency-writer, and hierarchy teardown correctly in their existing
`close()`/`aclose()`/`stop()`; registering the owner reuses that ordering instead of reinventing it.
Those methods must be made **idempotent**, since they may also be called normally.

`startup.main`'s interrupt-phase registration is why no separate `add_signal_handler` path is needed:
it captures main's locals in a closure and performs the schedule-and-flag subset (`close()`,
`cancel()` — pure state mutation plus a `call_soon`) that is safe mid-bytecode, leaving the awaits
for its threaded registration. It is not two shutdown handlers; it is one participant honouring the
same two-phase contract as everything else.

Supersedes and removes the three stale TODOs at
[history.py:279](src/aeth_ext/central_log_server/client/history.py#L279),
[id_checkpoint.py:85](src/aeth_ext/central_log_server/client/id_checkpoint.py#L85), and
[id_checkpoint.py:126](src/aeth_ext/central_log_server/client/id_checkpoint.py#L126), all of which
ask for exactly this.

### D-I9 — Collapse the `FATAL_EVENT` wait sites

With one state object, `while not FATAL_EVENT.is_set()` becomes `while not SHUTDOWN.is_set()` and
already fires on both kinds — a mechanical rename rather than six bespoke
`asyncio.wait(..., FIRST_COMPLETED)` dual-wait edits. This is the main practical payoff of D-I1.

Sites: [startup.py:132](src/aeth_ext/central_log_server/startup.py#L132),
[writer_thread.py:205](src/aeth_ext/central_log_server/server/writer_thread.py#L205),
[reader_server.py:229](src/aeth_ext/central_log_server/server/reader_server.py#L229),
[heartbeat.py:251,253,314,315](src/aeth_ext/monitoring/heartbeat.py#L251),
[test_entrypoint.py:192](src/aeth_ext/central_log_server/test_entrypoint.py#L192).

Preserve [heartbeat.py:315](src/aeth_ext/monitoring/heartbeat.py#L315)'s use of the `wait()` **return
value** to distinguish "woken by the event" from "timed out".

---

## Implementation order

1. `ShutdownKind` + `ShutdownState` in `err_handling.py` (D-I1).
2. `ShutdownPhase`, copy-on-write registry, `register_for_shutdown()`, two-pass `run_shutdown()`
   (D-I2, D-I3). Export all of them from `err_handling.__all__` and `aeth_ext.errors.__init__`.
3. Signal handler rewrite in `aeth_ext/__init__.py`, installed only under `-O` (D-I3).
4. Best-effort early-exit path (D-I4).
5. `RecordHistoryBuffer` lock + write-through + startup repair (D-I6, D-I7).
6. Register the eight participants; implement `AsyncioIdCheckpointBackend.close()` (D-I8).
7. Rename the wait sites (D-I9).
8. Update `TODO.md`; delete the three stale TODOs.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run pyright
```

`ShutdownState` is process-global and one-shot, exactly like the `FATAL_EVENT` it replaces: any test
that really triggers it must run in an isolated subprocess, following the existing pattern in
`tests/errors/_optimized_scenarios.py` + `tests/errors/test_err_handling.py`. Registration and
ordering tests (phase separation, priority sorting, `required` skip policy, copy-on-write reads) run
in-process against a throwaway state object without ever setting the real one.

End-to-end confidence comes from
[test_client_server_integration.py](tests/central_log_server/test_client_server_integration.py),
which spawns a real server subprocess and a real client.

Manual checks, in rough order of how much they prove:

- Append entries below the flush thresholds, send SIGTERM, confirm they reach the JSONL file.
- Confirm records emitted *during* the grace period also land — this is what proves write-through
  mode, not just the arm flag, actually works.
- Confirm the process exits before the grace period on the happy path, and that a deliberately wedged
  non-daemon thread degrades to SIGKILL without hanging anything else or corrupting the history file.
- Hand-craft a truncated final line and confirm the next `RecordHistoryBuffer` construction repairs
  it; separately hard-kill a process mid-write and confirm the same.
- Confirm Ctrl-C behaves normally under a non-`-O` interpreter.
