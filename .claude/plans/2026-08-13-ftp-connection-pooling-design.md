# Design: FTP/SFTP connection pooling in `FTPAdapter`

Design agreed 2026-08-13. Sub-project #2 of the
[shutdown-and-ftp master plan](2026-08-13-shutdown-and-ftp-master-plan.md). Independent of every
other sub-project in that plan; no blocking dependency in either direction.

## Context

While investigating sub-project #4 (scheduled-invoice-processor's shutdown redesign), we needed to
know how long an in-flight FTP batch could realistically take, to reason about shutdown time budgets.
A profiling spike (2026-08-13, instrumentation added and removed from
`scheduled_invoice_processor/suppliers/__init__.py`, never committed) measured a real batch: 77 files,
16-way concurrent (thread-pool-limited), ~5-7s per file, ~36s total wall time for the batch.

That confirmed two things:

1. **Transfers are already fully concurrent.** The `to_thread` + `gather()` pattern in
   `_pickup_files`/`_dropoff_files` genuinely parallelizes across the default executor's 16 workers.
   There is no serialization bug to fix here, and paramiko's per-channel thread-unsafety is a
   non-issue because every transfer already opens its own independent connection — nothing is shared
   across threads.
2. **The real, fixable cost is per-file connection setup.** `_transfer_file_vend_to_main` (and every
   other call site — `_transfer_file_main_to_main`, `_middle_archive_file`, `_vendor_archive_file`,
   `_pickup_files`'s `listdir` call) opens a **fresh** `start_session()` — a brand-new TCP connection
   plus FTP `USER`/`PASS` login or a full SSH handshake plus SFTP subsystem open — for every single
   file, even within one already-concurrent batch of dozens of files against the same server. That
   handshake cost (plausibly 1-3s of the observed 5-7s per file over a WAN link) is paid 77 times in a
   77-file batch instead of ~16 times (once per concurrent worker).

This document specs the fix: reuse connections across a batch (and across batches, and across the
whole process lifetime) instead of opening one per file.

## Non-goals

- **Not fixing FTP concurrency.** It already works. This is purely about connection reuse.
- **Not changing `AdapterProtocol`, `AdaptedFTP`, or `AdaptedSFTP`.** Both stay exactly as they are —
  thin wrappers around one `get_conn_handler()`-produced handler, with the same `upload_file`/
  `download_file`/`transfer_file`/`rename`/`remove`/`listdir`/`get_size`/`makedir`/`test_connection`
  surface. Pooling is invisible above `FTPAdapter`.
- **No new top-level classes.** Explicitly rejected during design: a separate `ConnectionPool` class
  was the initial draft, but pooling state and logic belongs directly on `FTPAdapter` — it's already
  the one-per-server, long-lived object this state naturally attaches to, and a wrapper class would
  just be indirection with no payoff.
- **No changes required in `scheduled-invoice-processor`** (or any other consumer). Every existing
  `with adapter.start_session() as client:` call site keeps working unmodified and gets pooling for
  free.

## Where the state lives

All new state and logic lives directly on `FTPAdapter` (`src/aeth_ext/ftp/adapter.py`). New
constructor parameters, both optional so existing call sites (`SFTFTPClient`,
`CoremarkFTPClient`, `SASSFTPClient`, `RYOSFTPClient` construction in `scheduled-invoice-processor`'s
`ftp_configs.py`) need no changes to keep working, just to opt into tuning:

```python
def __init__(
  self,
  ftp_protocol: type[FTPProtocol | SFTPProtocol],
  container_cls: str | None = None,
  pbar: Progress | None = None,
  tzinfo: ZoneInfo | None = SETTINGS.tz,
  container_cvar: ContextVar[str] | None = None,
  max_connections: int = 16,  # new — default matches to_thread's default executor worker count
  keepalive_interval: float | None = None,  # new — None (default) means keep-alive is off
):
```

New instance state:

- `_idle: queue.Queue[HandlerType_T]` — bounded (`maxsize=max_connections`) queue of live, currently
  unchecked-out protocol handlers (the objects `get_conn_handler()` returns — `FTP`/`SFTPClient`
  instances, held via the same `AdaptedFTP`/`AdaptedSFTP` wrapper as today).
- `_current_size: int`, guarded by a plain `threading.Lock` (`_size_lock`) — total connections open
  right now, idle or checked out. Needed because "idle queue is empty" and "at ceiling" are different
  conditions that both block a checkout.
- `_discovered_max: int | None` — `None` until a growth attempt is refused by the server, then set to
  the last successful `_current_size` (see "Checkout / release" for how it's used, and "Recovering a
  discovered ceiling" for how it can rise again later).
- `_discovered_max_last_probe: float` — monotonic timestamp of the last time a growth attempt was
  refused, used to gate re-probing (see "Recovering a discovered ceiling"). Only meaningful when
  `_discovered_max` is not `None`.
- `_keepalive_thread: Thread | None` — only created if `keepalive_interval` is not `None`.

## Checkout / release

**`start_session()`** becomes the checkout path:

1. Try `_idle.get_nowait()`. If it returns a handler, validate it lazily (see below) before handing it
   back wrapped in `AdaptedFTP`/`AdaptedSFTP` — on failed validation, close it, decrement
   `_current_size`, and fall through to step 2 as if the queue had been empty.
2. If the idle queue was empty (or the popped connection failed validation): under `_size_lock`, if
   `_current_size < effective_ceiling` (`min(max_connections, _discovered_max)` when `_discovered_max`
   is set, else `max_connections`), increment `_current_size` and open a new connection via the
   existing `ftp_protocol().get_conn_handler()` path. If that raises the same errors
   `get_conn_handler()` already translates (`ConnectionRefusedError`, `TimeoutError`, `gaierror` →
   `ServerNotAvailableError`, per `ftp_configs.py`'s existing per-consumer translation), treat it as
   discovering the real ceiling: decrement `_current_size` back down, set
   `_discovered_max = _current_size` (the last count that *did* succeed) and
   `_discovered_max_last_probe = monotonic()`, and fall through to step 3.
3. If at ceiling (or a growth attempt just failed): block on `_idle.get()` — this call already runs
   inside a `to_thread` worker for every real call site in `scheduled-invoice-processor`, so blocking
   here is fine; it does not stall the event loop.

The returned `AdaptedFTP`/`AdaptedSFTP` instance is unchanged; it just wraps a reused `self.handler`
instead of a fresh one.

**Release** (`AdaptedFTP.__exit__` / `AdaptedSFTP.__exit__`): today these unconditionally call
`self.proto_instance.close_conn_handler()`. Pooled behavior: return the handler to `_idle` via
`FTPAdapter`'s release method instead, **unless** `exc_val` indicates a connection-fatal failure, in
which case close it and decrement `_current_size` — same effect as today (a closed, discarded
connection), just with the accounting updated so the next checkout can grow a replacement.

**"Connection-fatal" classification**: reuse the same shape of check `suppliers/__init__.py`'s
`_is_transient_transfer_error` already uses (an isinstance check against `TimeoutError`,
`ConnectionError`, `BrokenPipeError`, `EOFError`, `SSHException`, plus substring matching against
`all_errors`), but as `FTPAdapter`'s own private classification — this lives in `aeth_ext`, a
different repo than `_is_transient_transfer_error`, so it is a parallel implementation kept
conceptually in sync, not shared code. A connection-fatal exception discards the handler; anything
else (e.g. a `FileNotFoundError` from a bad remote path) returns the handler to the pool as usual,
since the connection itself is still good.

## Recovering a discovered ceiling

`_discovered_max` is not pinned forever — a server's real connection limit can rise at runtime (an
admin raises a per-user quota without the app restarting), and a ceiling discovered once under
transient network trouble shouldn't be permanent either. Re-probing is rate-limited rather than
retried on every checkout, so a persistently-refusing server doesn't pay a failed-connection round
trip on every single checkout once it's found its limit:

- A checkout that would otherwise block at step 3 (idle queue empty, at `_discovered_max`) instead
  attempts one growth probe if at least `_REPROBE_INTERVAL` (default 300s) has elapsed since
  `_discovered_max_last_probe`.
- On success, `_discovered_max` is raised to the new `_current_size` (not cleared to `None` — the next
  refusal, if any, re-pins it at whatever the new real limit turns out to be) and the checkout proceeds
  with the newly-opened connection.
- On failure, `_discovered_max_last_probe` is reset to `monotonic()` (restarting the rate-limit window)
  and the checkout falls through to blocking on `_idle.get()` as usual.
- This probe is a normal `start_session()` caller's checkout, not a separate background mechanism —
  no new thread, just an occasional extra attempt inline on whichever caller's checkout happens to land
  after the rate-limit window has elapsed.

## Lazy validation on checkout

Before handing a pooled (not newly-opened) handler back to a caller, send one cheap round trip:

- **FTP** (`AdaptedFTP`): `handler.voidcmd("NOOP")`.
- **SFTP** (`AdaptedSFTP`): there is no true SFTP no-op; use `handler.listdir(".")` (cheapest
  operation that forces a real round trip and surfaces a dead channel) — cost is one directory listing
  of `.`, discarded.

On failure, close the handler, decrement `_current_size`, and proceed as if the idle queue had been
empty (open fresh, subject to the same ceiling logic as any other checkout). This only costs an extra
round trip in the (expected to be rare) case where a connection actually went stale between release
and the next checkout — freshly-opened connections skip this check entirely, since they were just
validated by successfully completing their handshake.

## Keep-alive (opt-in)

When `keepalive_interval` is set, a daemon `Thread` started lazily on first use (not in `__init__` —
mirrors the existing "register only once there's real state" convention documented in
`central_log_server`'s `AsyncioQueueDrainer`/`ThreadedQueueDrainer`) loops:

```python
while not stop_event.wait(timeout=keepalive_interval):
  try:
    handler = _idle.get_nowait()
  except Empty:
    continue  # nothing idle to keep warm right now
  else:
    # same validation path as checkout — ping and return, or discard on failure
    ...
```

This reuses the exact checkout/validate/release machinery rather than a separate ping mechanism — the
keep-alive thread is just another caller of the same internal validate-and-return helper, on a timer,
only ever touching connections that are currently idle (never steals a connection out from under an
in-flight transfer, since checked-out handlers aren't in `_idle`).

## Shutdown integration

`FTPAdapter` registers itself with `register_for_shutdown` (`ShutdownPhase.THREADED`, default
priority — runs before aeth_ext's own `LOGGING_TRANSPORT_PRIORITY` transport teardown, consistent with
every other downstream-application registrant) the first time a connection is opened (same
lazy-registration convention as the keep-alive thread). The registered callback:

1. Signals the keep-alive thread (if running) to stop via its `stop_event`, and joins it with a short
   bounded timeout.
2. Drains `_idle` completely, closing every connection found (`close_conn_handler()` on each).
3. Does **not** attempt to interrupt or wait on connections currently checked out — a transfer that's
   mid-flight when shutdown fires is the concern of sub-project #4 (the *application's* shutdown
   registration decides whether/how long to wait for in-flight FTP work), not this adapter. This
   registration's job is only to make sure idle, pooled connections don't leak past process exit.

## Testing

- Ramp-up: a fake `FTPProtocol` test double that succeeds up to N connections then raises
  `ConnectionRefusedError` on the (N+1)th — assert the pool discovers and pins `_discovered_max = N`
  and, within the `_REPROBE_INTERVAL` window, does not attempt to exceed it again.
- Recovery: same setup, but the test double is reconfigured to accept more connections partway through
  (simulating an admin raising the server-side quota without an app restart) and the test fast-forwards
  past `_REPROBE_INTERVAL` (inject/mock the clock rather than sleeping in real time) — assert the next
  checkout after that window successfully raises `_discovered_max`.
- Lazy validation: a test double whose handler's no-op call fails on the second checkout — assert the
  stale connection is discarded (not returned to the caller) and a fresh one is transparently opened
  in its place.
- Release classification: assert a connection-fatal exception inside a `with adapter.start_session()`
  block results in the handler being closed and `_current_size` decremented, while a non-fatal
  exception (e.g. `FileNotFoundError` from `get_size`) results in the handler being returned to
  `_idle`.
- Keep-alive: assert it only ever touches idle connections (never one concurrently checked out by
  another thread) and that disabling it (`keepalive_interval=None`, the default) spawns no thread at
  all.
- Shutdown: assert the registered callback closes every idle connection and does not block on/attempt
  to touch checked-out ones.
- Concurrency regression: a test asserting `max_connections` concurrent checkouts succeed without
  blocking each other, and a `max_connections + 1`th checkout blocks until one of the first N is
  released — guards the core pooling contract itself, independent of the ramp-up/backoff details.
