# Client transport composition refactor

Status: approved, not yet implemented
Date: 2026-08-06
Related: PR #11 review comment on `src/aeth_ext/central_log_server/client/__init__.py` (file-level,
[r3730808090](https://github.com/AetherBreaker/aeth_ext/pull/11#discussion_r3730808090)) — "All three of the
classes in this file duplicate eachothers code quite a bit. This should be deduplicated using composition
patterns (as opposed to an inheritance pattern)"

## Context

`src/aeth_ext/central_log_server/client/__init__.py` defines three client transport classes that each ship
records to the central log server: `HandshakeSocketHandler` (a `logging.handlers.SocketHandler` subclass,
lazy per-emit reconnect), `AsyncioQueueDrainer` (asyncio-based, drains an `asyncio.Queue`), and
`ThreadedQueueDrainer` (threaded, drains a `queue.Queue`). They duplicate a substantial amount of logic:

- **Emergency-mode state machine** — thresholds, failure counting, `EmergencyHistoryWriter` lifecycle.
  Byte-for-byte identical logic in all three.
- **Record bookkeeping** — assign id, wrap in `HistoryEntry`, `history.append()`,
  `id_checkpoint.schedule_persist()`, optionally hand to a local logger, optionally submit to the emergency
  writer. This exact sequence appears 5 times across the file (once in `HandshakeSocketHandler.emit`, twice
  in `AsyncioQueueDrainer`, twice in `ThreadedQueueDrainer`).
- **`_replay_backlog`** — identical `history.find_after(...)` + warn-on-gap logic in all three; only the
  "how do I put bytes on the wire" tail differs.
- **`_arm_shutdown`** — identical two-liner (`history.begin_shutdown(); id_checkpoint.begin_shutdown()`) in
  all three.
- **`_read_message`** — two of the three implementations (`HandshakeSocketHandler`, `ThreadedQueueDrainer`)
  are already 100% identical, blocking-socket-based; the third (`AsyncioQueueDrainer`) is the async
  equivalent. None of the three actually reference `self` in their bodies.

`HandshakeSocketHandler` must remain a `logging.handlers.SocketHandler` subclass — that's a stdlib contract
this refactor does not touch. The goal is to extract the aeth_ext-specific duplicated behavior into composed
components, not to replace that base-class relationship or unify the three classes into one.

## Goals

- Eliminate the duplicated logic listed above via composition: two new stateful components owned by each
  transport class, plus two stateless free functions.
- No change in public API or observable wire/durability behavior, except the specific, explicitly listed
  behavioral deltas below (which are bugfixes uncovered by the deduplication itself).
- Each new component is independently unit-testable without constructing a full client transport class.

## Non-goals

Left exactly as-is, because the concurrency mechanics are genuinely different per transport and unifying
them would hurt readability for negligible deduplication:

- The three reconnect loops (`HandshakeSocketHandler`'s lazy `createSocket`-triggered reconnect,
  `AsyncioQueueDrainer.run`, `ThreadedQueueDrainer._run`).
- `connect_and_verify` (three different connection-establishment mechanics).
- `_watch_apply_result` / `_check_apply_result` (background thread vs. asyncio task vs. non-blocking
  `select`-poll — three different concurrency models for ~3 shared lines of actual logic).
- The actual "put these bytes on the wire" calls (`sock.sendall(self.makePickle(...))` vs.
  `writer.write(...) + await writer.drain()` vs. `sock.sendall(LENGTH_STRUCT.pack(...) + payload)`).
- `emit` / `_transmit` (the `logging.Handler` interface contract; concept doesn't exist on the drainers).
- Overall teardown sequencing in `close`/`aclose`/`stop` (timeout/thread-join/loop-affine mechanics differ
  per transport) — these methods still exist per class, they just call into the new components' own
  `flush()`/`close()`/`arm_shutdown()` instead of duplicating the buffer/checkpoint logic inline.

## Architecture

Two new stateful components, each owned as an instance attribute by all three transport classes, plus two
free functions:

```text
client/
  emergency.py     -> EmergencyModeTracker
  durability.py    -> RecordDurability
  __init__.py      -> HandshakeSocketHandler, AsyncioQueueDrainer, ThreadedQueueDrainer
                       (+ read_server_message_sync / read_server_message_async, alongside the
                       existing _recv_exact helper)
```

`RecordDurability` is constructed first (it owns the `RecordHistoryBuffer`, which computes the per-program
`history_dir`); `EmergencyModeTracker` is constructed second, reading `history_dir` off it via a small
convenience property so callers never need to reach through `durability.history.history_dir`.

### `EmergencyModeTracker` (`client/emergency.py`)

```python
class EmergencyModeTracker:
  def __init__(
    self, history_dir: Path, program_name: str, *, time_threshold: float, attempt_threshold: int
  ) -> None: ...

  def record_failure(self) -> None:
    """Increment the consecutive-failure counter."""

  def record_success(self) -> None:
    """Reset the failure counter, stamp last-success time, and exit emergency mode if it was active."""

  def maybe_enter(self) -> None:
    """Check both thresholds; spin up an EmergencyHistoryWriter if they've tripped and one isn't already active."""

  @property
  def writer(self) -> EmergencyHistoryWriter | None: ...

  def close(self) -> None:
    """Close the writer if one is active."""
```

`time_threshold` is stored already converted to seconds (callers pass `emergency_time_threshold * 60.0`,
matching today's constructors). `record_success()` folds in what is today's separate
`_exit_emergency_mode()` call — every existing call site calls both back-to-back already, so there is no
behavior change, just one fewer method to call.

Kept fully decoupled from `RecordDurability` (only a `Path` and a `str` in its constructor, not a whole
durability object) so it can be unit-tested on its own against a `tmp_path`.

### `RecordDurability` (`client/durability.py`)

```python
class RecordDurability:
  def __init__(
    self,
    program_name: str,
    *,
    max_records: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_age: float = 300.0,
    id_checkpoint_backend: Literal["thread", "asyncio"] = "thread",
    event_loop: AbstractEventLoop | None = None,
  ) -> None: ...

  @property
  def history_dir(self) -> Path: ...

  @property
  def last_sent_id(self) -> int: ...

  def record(
    self, record: TaggedLogRecord, *, local_root: logging.Logger | None, emergency_writer: EmergencyHistoryWriter | None
  ) -> HistoryEntry:
    """Assign an id, build a HistoryEntry, append to history, schedule checkpoint persistence,
    optionally hand to a local logger, optionally submit to an active emergency writer. Returns the
    entry so the caller can transmit it."""

  def mark_sent(self, entry_id: int) -> None:
    """Record that entry_id has been confirmed on the wire."""

  def resolve_backlog(self, ack: HandshakeAck) -> tuple[HistoryEntry, ...]:
    """history.find_after(...) plus the warn-on-gap logging; returns () when there's nothing to replay
    or the gap is unrecoverable (both cases already log there and resume live)."""

  def arm_shutdown(self) -> None:
    """history.begin_shutdown(); id_checkpoint.begin_shutdown()."""

  def flush(self) -> None:
    """history.flush()."""

  def close(self) -> None:
    """id_checkpoint.close()."""
```

`id_checkpoint_backend`/`event_loop` preserve today's `HandshakeSocketHandler`-only choice unchanged; the
two drainer classes keep constructing with the default `"thread"`, matching their current fixed behavior
exactly.

`resolve_backlog` takes only `ack` (not `program_name` — the component already has it from construction,
so the parameter list doesn't repeat it).

### Free functions (added to `client/__init__.py`)

```python
def read_server_message_sync(sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None: ...
async def read_server_message_async(reader: asyncio.StreamReader, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None: ...
```

Lifted verbatim from the three existing `_read_message` method bodies (confirmed none of them reference
`self`). Replace `self._read_message(...)` call sites with the free-function call, passing the socket/reader
that was already available at each call site.

## Call-site mapping

| Today | Becomes |
| --- | --- |
| `self._history = RecordHistoryBuffer(...)` + `self._id_checkpoint = ...` + `self._next_id = ...` (x3 constructors) | `self._durability = RecordDurability(self._program_name, ...)` |
| `self._emergency_time_threshold`/`_consecutive_failures`/etc. fields (x3 constructors) | `self._emergency = EmergencyModeTracker(self._durability.history_dir, self._program_name, time_threshold=..., attempt_threshold=...)` |
| `_arm_shutdown` (x3, identical) | `self._durability.arm_shutdown()` |
| record-bookkeeping block (x5) | `entry = self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)` |
| `_maybe_enter_emergency_mode` (x3, identical) | `self._emergency.maybe_enter()` |
| `_exit_emergency_mode` + the `_consecutive_failures = 0` reset before it (x3) | `self._emergency.record_success()` |
| failure-path `self._consecutive_failures += 1` (x3) | `self._emergency.record_failure()` |
| `_replay_backlog`'s `find_after` + warn-on-gap (x3) | `self._durability.resolve_backlog(ack)`, transport keeps its own send loop over the result |
| `self._last_sent_id = entry.id` (in `_transmit` and all three `_replay_backlog`s) | `self._durability.mark_sent(entry.id)` |
| `self._history.flush()` (in close/aclose/stop) | `self._durability.flush()` |
| `self._id_checkpoint.close()` (in close/aclose/stop) | `self._durability.close()` |
| `self._emergency_writer.close()` (in close/aclose/stop) | `self._emergency.close()` |
| `self._read_message(...)` (sync, x2 identical) | `read_server_message_sync(sock, ...)` |
| `self._read_message(...)` (async, x1) | `await read_server_message_async(reader, ...)` |

## Behavioral deltas (intentional, uncovered by dedup)

1. **`HandshakeSocketHandler`'s emergency-enter log message gains the program name.** Today only the two
   drainer classes' `_maybe_enter_emergency_mode` include `%r` program name in the "Log server unreachable
   for..." warning; `HandshakeSocketHandler`'s omits it. `EmergencyModeTracker.maybe_enter()` always has a
   program name (constructor requires it) and always includes it — a strict improvement, not a behavior
   users could be relying on the absence of.
2. **Side-effect order in the two "already in emergency mode" bulk-drain loops**
   (`AsyncioQueueDrainer._sleep_or_drain`, `ThreadedQueueDrainer._sleep_or_drain`) changes from
   (emergency-submit, then local-handle) to `record()`'s fixed order (local-handle, then emergency-submit) —
   matching the other three call sites. Both effects are independent (writing to the emergency file and
   handing off to a local logger don't interact), so this has no functional consequence.

## Testing

- New `tests/central_log_server/test_client_emergency.py` — unit tests for `EmergencyModeTracker` against
  a `tmp_path`, no client transport class involved. Matches the existing flat `test_client_<concern>.py`
  naming convention (`test_client_history.py`, `test_client_id_checkpoint.py`, `test_client_filters.py`).
- New `tests/central_log_server/test_client_durability.py` — unit tests for `RecordDurability`, similarly
  isolated (fake `TaggedLogRecord`s, `tmp_path`).
- Existing `test_client.py`, `test_client_queue_drainers.py`, `test_client_server_integration.py`,
  `test_client_history.py` should require no *behavioral* changes — same public API, same wire/durability
  behavior — but will need monkeypatch-target updates anywhere they currently reach into
  `HandshakeSocketHandler`/`AsyncioQueueDrainer`/`ThreadedQueueDrainer` internals that move onto the new
  components (e.g. anything patching `_emergency_writer`, `_consecutive_failures`, or similar attributes
  directly rather than through behavior).
