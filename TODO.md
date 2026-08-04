# TODO

Deferred issues found during the 2026-08-04 investigation into the central log server's
intermittent full-process hang.

The **primary** issue from that investigation (the `QueueForwardHandler.green_put` event-loop
deadlock) is *not* listed here — it is being worked on separately. Everything below is a real,
independent defect that was found along the way, none of which is required to explain the hang,
though items 1 and 3 can contribute to or widen it.

---

## 1. ~~Blocking network + file I/O runs directly on the main asyncio event loop (heartbeat)~~ — DONE

**Resolved 2026-08-04.** `send_heartbeat`'s body was extracted into a private `_send_heartbeat`
primitive that takes an already-resolved slug, and two call sites now offload it with
`asyncio.to_thread`:

- new public `send_heartbeat_async()` (exported from `aeth_ext.monitoring`), used by
  `startup.main`'s boot-time "start" ping;
- `_run_heartbeat_async`'s per-interval ping.

Each offload is **awaited**, so a wedged ping can never tie up more than one worker thread — the
next heartbeat is not dispatched until the current one returns. `HeartbeatThread.run` now calls
`_send_heartbeat` directly (it is already a dedicated thread, and it had the same wrong-frame
re-resolution hazard described below).

Slug auto-detection is preserved: `send_heartbeat_async` resolves `HEARTBEAT_SLUG` from **its own**
caller's frame *before* handing work to a thread that no longer has that caller on its stack.
`_send_heartbeat` never re-resolves, which also removes a latent wrong-frame lookup that existed
in the old code whenever `slug` came through as `None`.

Covered by four new tests in `tests/monitoring/test_heartbeat.py` (two per entry point: work runs
off the loop thread, and a held ping does not stall the loop — asserted on elapsed wall-clock, not
tick count, since a stalled loop still completes every tick eventually).

**Residual, not fixed:** `urlopen`'s timeout still does not cover DNS resolution, so an individual
ping can still hang indefinitely — it just can no longer take the event loop with it. The
externally visible effect of a wedged resolver is now the correct one: heartbeats stop (so the
dead-man's switch fires) while the log server keeps serving. One further consequence worth
knowing: a permanently hung ping thread will delay interpreter exit, because `asyncio.run` waits
on `loop.shutdown_default_executor()` (300s cap in CPython) at teardown.

---

## 1b. Original analysis (retained for context)

**Severity:** high — independently capable of hanging or badly stalling the reader server's loop.

**Where:**

- `src/aeth_ext/monitoring/heartbeat.py` — `send_heartbeat()`, lines ~102-113
- `src/aeth_ext/monitoring/ping.py` — `ping_healthcheck()`, line ~56
- Called from `_run_heartbeat_async()` (`heartbeat.py` ~line 165 and ~line 171), which is
  scheduled as `periodic_heartbeat_task` on the *main* event loop in
  `src/aeth_ext/central_log_server/startup.py` line ~111.

**What's wrong:**

`_run_heartbeat_async` is an `async def` coroutine, but the work it actually performs is fully
synchronous and is executed inline on the event loop thread:

```python
# heartbeat.py, inside send_heartbeat()
heartbeat_file.write_text(datetime.now(tz).isoformat())   # blocking file I/O
...
ping_healthcheck(resolved_url, start=start, failure=failure, autoprovision=autoprovision)

# ping.py
with urlopen(ping_url, timeout=_REQUEST_TIMEOUT_SECS):     # blocking HTTP request
    pass
```

Two distinct problems:

1. **`urlopen`'s `timeout` does not cover DNS resolution.** `urlopen` →
   `http.client.HTTPSConnection.connect()` → `socket.create_connection((host, port), timeout)`,
   and `create_connection` calls `socket.getaddrinfo()` *before* the timeout is applied to any
   socket. `getaddrinfo` is a blocking C call into the system resolver with no timeout argument.
   In a container with a wedged or unreachable DNS server this can block far longer than
   `_REQUEST_TIMEOUT_SECS` (and on some libc/resolver configurations, effectively indefinitely).
   While it blocks, **the entire reader-server event loop is frozen** — no socket accepts, no
   record reads, no heartbeat.
2. **Even on the happy path it is a stall.** `_REQUEST_TIMEOUT_SECS = 10` and
   `DEFAULT_HEARTBEAT_INTERVAL_SECS = 60`, so a slow/unreachable healthchecks.io endpoint parks
   the loop for up to 10 seconds out of every 60 — ~17% of wall time with the server unable to
   read a single log record.

The local file write is a smaller but real version of the same problem: `HEARTBEAT_FILE` lives
under `settings.log_loc_folder`, which in the Coolify deployment is a mounted volume. A stalled
mount blocks the loop thread.

**Note on the "no alert was sent" symptom:** this is *not* the cause of that. The heartbeat task
being blocked in `urlopen` still leaves the process alive with `FATAL_EVENT` unset, so no alert
would fire — consistent with what was observed. Worth keeping in mind that this issue and the
primary deadlock produce indistinguishable external symptoms.

**Fix direction:** see the resolution note above.

---

## 2. The main entrypoint silently discards its uvloop/winloop event loop

**Severity:** medium — a permanent, silent performance regression plus a leaked loop object.

**Where:**

- `src/aeth_ext/central_log_server/__main__.py` lines 30 and 43
- `src/aeth_ext/__init__.py` — `initialize()`, lines ~62-76

**What's wrong:**

`initialize(asyncio=True)` builds an optimized loop and installs it as the thread's current loop:

```python
# aeth_ext/__init__.py
if platform in ("win32", "cygwin", "cli"):
    from winloop import new_event_loop
else:
    from uvloop import new_event_loop
from asyncio import set_event_loop
set_event_loop(new_event_loop())
```

But `__main__.py` then does:

```python
initialize(asyncio=True, logging=False)   # line 30
...
run(main(**kwargs))                       # line 43 — plain asyncio.run
```

`asyncio.run()` **ignores `set_event_loop()`**. It unconditionally creates a fresh loop from the
default policy (via `Runner`), runs on that, and closes it. Consequences:

- The central log server's main loop is a **stock `SelectorEventLoop`**, not winloop/uvloop —
  the optimization the code believes it is applying is not applied at all.
- The winloop/uvloop instance created by `initialize()` is **never run and never closed**, so its
  file descriptors / handles leak for the life of the process.

This exact pitfall is already documented and correctly handled elsewhere in the codebase — see
`LogWriterThread.run()` in `src/aeth_ext/central_log_server/server/writer_thread.py` lines
~167-185, which explains the problem and passes `loop_factory=_new_event_loop` explicitly. The
writer thread gets uvloop; the main server does not.

**Fix direction:**

Either pass `loop_factory` at the `asyncio.run` call site in `__main__.py` the same way
`LogWriterThread.run` does, or better, have `initialize(asyncio=True)` return / expose the
`new_event_loop` callable so callers can hand it to `asyncio.run(..., loop_factory=...)` instead
of installing a loop that gets thrown away. Audit every other `initialize(asyncio=True)` call
site in the repo and in downstream consumers for the same mistake before changing the contract.

---

## 3. `build_hierarchy()` performs blocking file I/O on the reader server's event loop, under the global `logging._lock`

**Severity:** medium-high — a loop stall on every client handshake, and it couples the main loop
to a lock the writer thread also contends for.

**Where:**

- `src/aeth_ext/central_log_server/server/reader_server.py` line ~136, inside `_handle_client`:
  ```python
  manager, root = build_hierarchy(handshake.config, self.log_dir / handshake.program_name)
  ```
- `src/aeth_ext/central_log_server/server/dispatch.py` — `build_hierarchy()`, lines 48-63

**What's wrong:**

`build_hierarchy` calls `dict_config(...)`, i.e. a full `dictConfig`-style configuration pass.
That is synchronous and does real work on the loop thread:

- instantiates every handler in the remote config, which for file handlers means `mkdir` and
  opening files on disk (a mounted volume in the Coolify deployment),
- and CPython's `DictConfigurator.configure()` wraps essentially its entire body in
  `logging._acquireLock()` / `logging._releaseLock()` — the **process-global** logging lock.

Two consequences:

1. Every client handshake blocks the reader server's event loop for the duration of the disk
   work. Under a reconnect storm (N clients reconnecting after a network blip) this serializes
   into a visible stall.
2. While the main loop holds `logging._lock`, the **writer thread** freezes if it touches it —
   which it does routinely: `_dispatch()` calls `entry.manager.getLogger(record.name)`
   (`writer_thread.py` line ~329) and `shutdown_hierarchy()` calls `handler.close()`
   (`dispatch.py` line ~102), both of which acquire the global `logging._lock`. Note that
   `logging._lock` is process-global and is **not** per-`Manager`, so the "private hierarchies
   need no lock" design note in the class docstrings does not exempt this path.

This is also a *contributing factor* to the primary deadlock: it widens the window in which the
main loop thread is busy/blocked while another main-loop task is parked waiting on the aiologic
queue token.

**Fix direction:**

Move `build_hierarchy` off the loop with `asyncio.to_thread`. This must preserve the current
fail-fast ordering — the hierarchy is deliberately built *before* the `HandshakeAck` is sent so
an invalid remote config is rejected at handshake time (see the comment at `reader_server.py`
line ~133). Awaiting a thread there keeps that ordering intact. Consider also whether hierarchy
construction belongs on the writer thread entirely, since the writer is the sole owner of every
hierarchy once registered — that would remove the cross-thread handoff of `manager`/`root`
through `RegisterClient` as well.

---

## 4. `_register_client` / `_unregister_client` do synchronous file I/O on the writer thread's event loop

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

## 5. `handle_fatal_exc_async` swallows `GeneratorExit` silently

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
(`FATAL_EVENT.is_set()`) from "collected unexpectedly" and alerting only on the latter, so a
normal shutdown stays quiet.
