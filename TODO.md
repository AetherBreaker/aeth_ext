# TODO

Deferred issues found during the 2026-08-04 investigation into the central log server's
intermittent full-process hang.

The **primary** issue from that investigation (the `QueueForwardHandler.green_put` event-loop
deadlock) is *not* listed here — it is being worked on separately. Items 1-4 below are the
remaining open, independent defects found along the way. (Three other items originally listed
here — blocking I/O in the heartbeat, the discarded uvloop/winloop policy, and blocking I/O in
`build_hierarchy` — have since been resolved and are no longer tracked in this file.)

---

## 1. `_register_client` / `_unregister_client` do synchronous file I/O on the writer thread's event loop

**Severity:** low-medium — stalls the writer's loop, which is meant to stay responsive.

**Where:** `src/aeth_ext/central_log_server/server/writer_thread.py`

- `_register_client()` lines 254-270
- `_unregister_client()` lines 272-287
- `_emit_connection_separator()` lines 289-305
- `_write_midnight_baseline()` lines 363-377 (called from `_update_id_registry`, line ~352)
- `_load_midnight_baseline()` lines 379-395 (constructor path only — acceptable)

**What's wrong:**

`LogWriterThread` hosts its own event loop specifically so that file I/O never blocks it — the
class docstring says records are "handed off to a worker thread via `asyncio.to_thread` so file
IO never blocks the loop", and `_dispatch` does honour that (line 330). But the
register/unregister paths do not:

- `_emit_connection_separator` → `handler.emit_separator(message)` → the monkey-patched
  implementation in `src/aeth_ext/central_log_server/patches.py` lines 35-64 does
  `stream.write(...)` **and** `self.flush()` synchronously, for every handler in the hierarchy.
- `_unregister_client` → `shutdown_hierarchy()` → `handler.flush()` + `handler.close()` for every
  handler, synchronously (`dispatch.py` lines 84-104).
- `_register_client` → `shutdown_hierarchy(stale...)` on the replace-a-stale-hierarchy path.
- `_write_midnight_baseline` writes a temp file and `replace()`s it on the loop thread, once per
  day-rollover.

All of this runs directly in `_process()` on the writer's event loop. While it runs, the writer
is not draining the queue and (per the primary issue) is not releasing the queue token to
producers.

**Fix direction:**

Wrap the I/O-performing bodies in `asyncio.to_thread`. Note the ordering constraint that makes
the current design correct: the writer is the *sole* owner of every hierarchy, so no locking is
needed, and register/unregister events are ordered behind that program's records in the FIFO so
nothing in flight is dropped. Any threading change must not allow a dispatch for program X to
overlap with X's teardown — awaiting the thread inline (rather than fire-and-forget) preserves
this.

---

## 2. `handle_fatal_exc_async` swallows `GeneratorExit` silently

**Severity:** low — an observability hole, not a hang, but it can hide exactly the class of
failure being chased.

**Where:** `src/aeth_ext/errors/err_handling.py` lines 269-270

```python
except GeneratorExit:
    return None  # if a GeneratorExit is caught, that means a coroutine is being cancelled for a graceful shutdown.
```

**What's wrong:**

The comment's assumption is too narrow. `GeneratorExit` is thrown into a coroutine by
`coro.close()`, which happens on *graceful shutdown* but also when a suspended coroutine is
**garbage-collected** — e.g. a task that was dropped without a strong reference, or a coroutine
that was created and never awaited. In those cases the decorated coroutine vanishes with:

- no log record,
- no alert e-mail / Pushover,
- `FATAL_EVENT` left unset.

Every long-lived coroutine in the log server is wrapped by this decorator (`_amain`,
`_handle_client`, `_run_heartbeat_async`), so a silently-collected task is indistinguishable
from a hang from the outside. It also swallows `GeneratorExit` rather than re-raising it, which
is a PEP 342 violation — a generator/coroutine that catches `GeneratorExit` and returns instead
of re-raising causes `RuntimeError: coroutine ignored GeneratorExit`.

Also relevant to the same observability gap: line 283,
`return func if __debug__ and __name__ != "__main__" else wrapper` — under `__debug__` the
decorator is a complete no-op, so none of this behaviour exists in local development. The
production path (`python -O`) is the only one that exercises the wrapper.

**Fix direction:**

At minimum, log at WARNING/ERROR before returning, and re-raise `GeneratorExit` so the
interpreter's contract is respected. Consider distinguishing "shutdown in progress"
(`SHUTDOWN.is_set()`) from "collected unexpectedly" and alerting only on the latter, so a
normal shutdown stays quiet.

---

## 3. `iter_unique_handlers` has no direct test

**Severity:** low — coverage gap on a function already in `__all__` and used in production code.

**Where:** `src/aeth_ext/central_log_server/server/dispatch.py` — `iter_unique_handlers()`; used by
`_emit_connection_separator` in `src/aeth_ext/central_log_server/server/writer_thread.py`.

**What's wrong:**

Deferred out of `PLAN-two-phase-logging-config.md` (D-F2). `iter_unique_handlers` deduplicates
handlers shared across multiple loggers in a private hierarchy (e.g. one attached to both `root`
and a child logger) so callers visit each exactly once. It is exercised indirectly through
`_emit_connection_separator`'s tests, but has no test asserting its dedup behaviour directly —
e.g. that a handler attached to both `root` and a child logger is yielded exactly once, in a
stable order.

**Fix direction:**

Add a small direct test in `tests/central_log_server/test_dispatch.py` building a private hierarchy
with a shared handler and asserting `list(iter_unique_handlers(manager, root))` yields it once.

---

## 4. Use heartbeat staleness to inform shutdown timeout budgets

**Severity:** enhancement — not a known bug, a robustness idea raised 2026-08-06.

**Where:** `src/aeth_ext/errors/shutdown.py` (`_handle_shutdown_signal`, `_run_threaded_pass`,
`_BUDGETS`); `src/aeth_ext/monitoring/heartbeat.py` (`run_heartbeat_async`, `HeartbeatThread`).

**The idea:**

The shutdown signal handler currently always requests `ShutdownKind.GRACEFUL` (unless something
upstream already escalated it), giving the threaded pass the full 7s budget regardless of whether
the process is actually capable of making progress. If a heartbeat file is in use, its last-written
timestamp is a cheap, already-existing liveness signal: if it's older than its expected write
interval plus a grace slack, that's evidence something is hung *before* the threaded pass even
starts, and the shutdown sequence can act on that assumption up front rather than discovering it by
burning the full graceful budget on registrants that were never going to complete.

The two heartbeat mechanisms imply different severities and shouldn't be collapsed into one rule:

- A stale timestamp from `run_heartbeat_async` (a coroutine on a specific event loop) only proves
  *that loop* is wedged — other loops/threads in the process may be fine.
- A stale timestamp from `HeartbeatThread` (a dedicated thread blocked only on `Event.wait()`) is
  stronger evidence of interpreter-wide trouble — if even that hasn't fired, the GIL is likely held
  uninterruptibly somewhere, or the interpreter itself is in trouble.

**Fix direction:**

- Add a registration surface on the shutdown side (e.g. `register_liveness_probe(path, interval)`)
  that `start_heartbeat_thread`/`run_heartbeat_async` call into when they start — `heartbeat.py`
  already imports from `errors` (for `SHUTDOWN`, `handle_fatal_exc_*`), so the dependency has to
  flow that direction, not the reverse.
- `_handle_shutdown_signal` reads the probe(s) and, if stale beyond `interval + slack`, calls
  `SHUTDOWN.request(ShutdownKind.FATAL)` instead of `GRACEFUL` — `_run_threaded_pass` already
  re-reads `SHUTDOWN.kind` every iteration (shutdown.py line ~367), so the budget shrink falls out
  for free with no new plumbing.
- Guard against false positives from ordinary scheduling jitter (threshold should not be the raw
  interval) and from the startup window (`send_start=True` already writes an initial heartbeat
  immediately, so "never written yet" shouldn't misfire as a hang).
- Reading the heartbeat file inside `_handle_shutdown_signal` is fine even though it blocks: unlike
  a raw OS/C-level signal handler, Python defers signal delivery to a safe point between bytecodes,
  so ordinary (fast, local) file I/O there is not a correctness hazard the way it would be in a true
  async-signal-safe handler.

---

## 5. Project-wide sweep for stale/plan-scoped code comments

**Severity:** maintainability — no behavioral bug, but stale comments actively mislead future work
(including future review by this assistant).

**Where:** whole codebase.

**What's wrong:**

Several now-deleted `PLAN-*.md` docs left comments in source that reference plan step IDs (e.g.
`D-I1`, `D-D5`, `D-F2`), design decisions, or reasoning that only made sense *during* implementation
of that plan.
Once a plan lands, those references stop being useful context and start being noise — or worse,
actively wrong once the surrounding code shifts again and the comment doesn't get updated to match.
More generally, the codebase should be swept for any comment describing intent/reasoning that no
longer matches what the code actually does (renamed things, removed branches, superseded designs),
not just plan-ID references specifically.

**Fix direction:**

- Grep for plan-ID patterns (`D-[A-Z]\d`) and similar markers across `src/` and `tests/`, and for
  each hit decide: delete the reference outright (if the comment's remaining content stands on its
  own), rewrite the comment to describe *why* the code is the way it is without the plan-implementation
  framing, or delete the whole comment if it no longer adds anything a reader couldn't get from the
  code itself.
- While doing that pass, also read every comment for staleness independent of plan references —
  anything describing behavior, callers, or reasoning that no longer holds.
- This is a comment-only cleanup; no behavioral changes should ride along with it, which makes it a
  good candidate for its own isolated PR/commit rather than folding it into unrelated work.

---

## 6. Replace `extract_details_callable` with a standardized fatal-exception-origin API

**Severity:** enhancement — no known bug, but the current parameter is a narrow one-off left open
for a single anticipated consumer.

**Where:** `src/aeth_ext/errors/err_handling.py` — `handle_fatal_exc_sync` (lines ~241-271) and
`handle_fatal_exc_async` (lines ~285-319), both taking an `extract_details_callable` keyword arg.

**What's wrong:**

`extract_details_callable` is an arbitrary `Callable[[BaseException], Any]` invoked with the caught
exception, added so *one specific consumer* could attempt to capture details about where a fatal
exception originated. It has no real production caller yet (only test doubles in
`tests/errors/_optimized_scenarios.py` and `tests/errors/test_err_handling.py` exercise it), and as
an unconstrained callable it doesn't generalize — every consumer would have to reimplement its own
frame-walking/module-matching logic.

**Fix direction:**

Design and add a standardized API for answering "did this fatal exception originate from within a
given module/package (or set of modules), including its submodules?" — e.g. inspecting
`exc.__traceback__` frames' `__module__`/`co_filename` against a caller-supplied package prefix (or
set of prefixes). Replace `extract_details_callable` with this purpose-built check once it exists,
rather than continuing to expose a raw callable escape hatch.

---

## 7. Replace `ShutdownState` with a more capable `aiologic.Event` subclass/recreation

**Severity:** enhancement — no known bug, current state exposed today is enough to build the
alert-then-shutdown paths, but it's a thin surface for anything wanting to *watch* shutdown rather
than just check or drive it.

**Where:** `src/aeth_ext/errors/shutdown.py` — `ShutdownState` (currently backing the module-level
`SHUTDOWN`).

**What's wrong:**

`ShutdownState` is a bespoke class layered over what is fundamentally event-like state (has it
happened yet, what kind, wait for it) rather than being built on `aiologic.Event`'s own primitives.
That means anything wanting richer detection/observation of a shutdown in progress — e.g. awaiting
a specific `ShutdownKind`, subscribing to a callback on transition, distinguishing "requested" from
"in progress" from "complete" as distinct waitable states — has no reusable primitive to build on
and would have to be bolted onto `ShutdownState` ad hoc.

**Fix direction:**

Investigate whether `ShutdownState` should become a subclass of `aiologic.Event` (inheriting its
wait/set primitives directly) or a fuller from-scratch recreation of the same tools `aiologic.Event`
provides, purpose-built for shutdown's specific states/kinds. Either way, the goal is a more
thorough suite of tools for detecting and watching for shutdown transitions than the current
single-state check/wait surface offers.

---

## 8. Interrupt-time emergency logging: an `emergency_fd` handler protocol plus a `SocketHandler` priority slot

**Severity:** enhancement — no known bug. Raised 2026-08-11 while designing the shutdown
confirm/force ladder, and deliberately deferred out of that work so the ladder could land alone.

**Where:** `src/aeth_ext/logging/bases.py` (handler base classes);
`src/aeth_ext/central_log_server/client/__init__.py` (`HandshakeSocketHandler`, `send`/`emit`);
`src/aeth_ext/logging/config/__init__.py` (`_configure_queue_handler`, line ~920);
`src/aeth_ext/logging/setup.py` (the `listener` walk, line ~289);
`src/aeth_ext/errors/shutdown.py` (`_emit`).

**What's wrong:**

Nothing the shutdown sequence emits during the *interrupt* phase can go through the logging system,
so it goes only to the raw diagnostic fd. That is correct and deliberate — but it means the interrupt
phase's diagnostics never reach the central log server, the log files, or anything else the operator
normally reads. The one place they'd be most valuable (a shutdown where the transport is the thing
that failed) is the one place they're absent.

The reason logging is barred there is worth restating precisely, because it is *not* the level checks
or the formatting — it is the lock inside the writer:

- **`queue.Queue`** is the configured default for `QueueHandler` (`config/__init__.py` line ~920:
  `q = queue.Queue()  # unbounded`). Its `put` takes a plain non-reentrant `threading.Lock` via its
  `Condition`. A signal landing while the main thread is inside `Queue.put` self-deadlocks the main
  thread — a permanent hang with no teardown at all. Note `queue.SimpleQueue` is C-implemented and
  documented reentrant-safe for exactly this reason; the two are not interchangeable here.
- **`Handler.lock` and `logging._lock` are `RLock`s**, so same-thread re-entry does *not* deadlock.
  This is a trap, not a reassurance: it makes the dangerous path look tested right up until the
  `Queue.put` above.
- **`BufferedWriter`** has reentrancy detection (`_enter_buffered_busy`), so a same-thread re-entrant
  `stream.write()` raises `RuntimeError: reentrant call inside <_io.BufferedWriter>` — the write is
  *lost* rather than hanging. From another thread it blocks instead.
- **`HandshakeSocketHandler`** (our own; stdlib `SocketHandler.send`/`makePickle` are *not* used —
  `emit` and `makePickle` are both overridden) writes one frame per record: a 4-byte big-endian
  length from `LENGTH_STRUCT` (`protocol.py` line 19, `struct.Struct(">L")`) followed by an
  `orjson.dumps` payload, in a single `sendall`. `sendall` releases the GIL and loops over partial
  sends, and under PEP 475 a Python signal handler runs *between* those partial sends. An emergency
  `sendall` on the same socket therefore splices its bytes into the middle of another record's frame,
  and the receiver reads a garbage length prefix from then on — the stream is desynchronized
  **permanently**, not for one record.

**Fix direction:**

Two pieces, useful independently but designed together.

*(a) An `emergency_fd()` handler protocol.* Add an optional method returning the raw file descriptor
a handler can safely be `os.write`-ten to during interrupt context, or `None` if it has none:

```python
def emergency_fd(self) -> int | None: ...
```

- `FileHandler`/`StreamHandler` return `self.stream.fileno()`. Writing the fd bypasses the buffer
  entirely, so neither the reentrancy `RuntimeError` nor the cross-thread block applies. The cost is
  possible interleaving with buffered content, which is tolerable in a text log.
- `QueueHandler` returns `None`; instead the collector walks to `listener.handlers` (the project
  already locates listeners this way in `setup.py` line ~289) and asks each destination directly.
  **Caveat to design around:** the `QueueListener` thread is concurrently draining into those same
  handlers, so bypassing the queue does not avoid contention — it guarantees it. This is precisely why
  the answer must be an fd rather than a call into `emit`.
- `HandshakeSocketHandler` returns `None` unless (b) is implemented. The protocol's real value is
  giving a handler a way to answer *honestly that it cannot do this safely*.

Shutdown collects the fds once at install time and, at interrupt time, does N `os.write` syscalls and
nothing else. This is the same approach `faulthandler.enable(file=...)` takes, and for the same
reason: an fd is the only writable thing when the interpreter's normal machinery is compromised.
Prebuilding the formatted bytes at install time is worthwhile here, since `Formatter.format` is the
expensive and unpredictable part.

*(b) A priority slot in `HandshakeSocketHandler`, immediately in front of its framing `sendall`.*
This is what lets the socket handler participate at all, and it provides a guarantee nothing else
can: *this record goes next*. In write-through mode a normally-emitted shutdown marker queues
**behind** the existing backlog, so with a large buffer it is a footnote rather than a marker; the
slot jumps it.

- **There is no single insertion point today.** Because the handler bypasses stdlib
  `SocketHandler.send`, three sites write frames directly: `_transmit` (line ~446, the steady-state
  path), `_replay_backlog` (line ~400), and `_send_handshake` (line ~334). Funnel all three through
  one private send method first, then the slot has exactly one drain site instead of three that can
  drift apart.
- Insertion must be lock-free — an atomic attribute store, a `deque.append` (atomic under the GIL),
  or `queue.SimpleQueue.put` (reentrant-safe by documentation). Draining happens on the emit path,
  already serialized under `Handler.lock`, giving a clean lock-free-producer / serialized-consumer
  split. A signal landing mid-`sendall` then appends instead of splicing, so frame integrity holds.
- **Peek-send-pop, never pop-send** — but note the reason is *not* that records get dropped when the
  server is down. They do not: `emit` puts every record into history via `self.record(...)` (line
  ~423) **before** attempting delivery, and `_replay_backlog` resends by id after a reconnect, so an
  ordinary record survives an outage. The priority slot is the exception precisely because it
  bypasses that machinery — history is file I/O and therefore not interrupt-safe, so a slot frame
  cannot be journalled on the way in. It is the one record in the system with no replay path, which
  is exactly why it must not be consumed until a `sendall` actually succeeds. Worth stating in the
  eventual docstring that the slot trades durability for immediacy.
- **Name the drainers explicitly.** The slot only empties on the next `send()`, and a shutdown
  deliberately stops emitting records, so a slot with no guaranteed drainer is a silent black hole.
  Two already exist and must be treated as load-bearing rather than incidental: the single
  `logger.critical` shutdown marker at the top of `_run_threaded_pass`, and `self.close` registered
  at `LOGGING_TRANSPORT_PRIORITY` (`client/__init__.py` line ~274) as the backstop.
- Bound the slot (e.g. `deque(maxlen=...)`) and decide which end is dropped on overflow. In practice
  the shutdown marker is one record per process lifetime, so headroom is ample.
- Unlike (a), prebuilding buys little here: the work is `pickle.dumps` of a dict — lock-free and tens
  of microseconds, so safe to do at interrupt time — and prebuilding costs timestamp fidelity, since
  the record's `created` would be stamped at install time and the log would misreport when the
  shutdown began.

**Forward compatibility:** the shutdown design this was deferred out of needs no rework to adopt it.
`_emit(text)` currently encodes and writes to one fd; it becomes a write to a list of fds, and
nothing else in the shutdown sequence moves.
