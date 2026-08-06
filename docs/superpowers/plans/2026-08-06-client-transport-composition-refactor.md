# Client Transport Composition Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduplicate the emergency-mode state machine, record-bookkeeping sequence, backlog-replay logic,
and server-message reading that are currently copy-pasted across `HandshakeSocketHandler`,
`AsyncioQueueDrainer`, and `ThreadedQueueDrainer` in `src/aeth_ext/central_log_server/client/__init__.py`,
by extracting them into two composed components and two free functions.

**Architecture:** Two new stateful classes (`EmergencyModeTracker` in `client/emergency.py`,
`RecordDurability` in `client/durability.py`), each owned as a plain instance attribute by all three
transport classes, plus two stateless free functions (`read_server_message_sync`/`read_server_message_async`)
added to `client/__init__.py` itself. No inheritance changes; `HandshakeSocketHandler` keeps its
`logging.handlers.SocketHandler` base class untouched.

**Tech Stack:** Python 3.14, pytest (`asyncio_mode = "auto"`), ruff, pyright.

## Global Constraints

- Do not use `from __future__ import annotations` anywhere (project-wide rule).
- 2-space indentation, matching the rest of this codebase.
- No behavior change except the two explicitly documented deltas in the spec (program name now included
  in `HandshakeSocketHandler`'s emergency-enter log message; minor side-effect reordering in the two
  "already in emergency mode" bulk-drain loops) — both no-ops functionally.
- Conventional Commits for every commit: `<type>(<scope>): <summary>`, `refactor` type, `central_log_server`
  scope (per `.claude/CLAUDE.md`).
- Only run the specific test files relevant to each task while it's in progress; run the full
  `tests/central_log_server` subtree once at the very end (Task 7), per this repo's testing-workflow
  convention — do not run the whole-repo suite eagerly on this feature branch.
- Full spec at `docs/superpowers/specs/2026-08-06-client-transport-composition-design.md` — re-read it if
  anything here seems to contradict it.

---

## Task 1: `EmergencyModeTracker` component

**Files:**
- Create: `src/aeth_ext/central_log_server/client/emergency.py`
- Test: `tests/central_log_server/test_client_emergency.py`

**Interfaces:**
- Consumes: `aeth_ext.central_log_server.client.history.EmergencyHistoryWriter` (existing, unchanged
  constructor `EmergencyHistoryWriter(history_dir: Path)`, methods `submit(entry: HistoryEntry)`,
  `close()`).
- Produces (used by Tasks 4-6):
  ```python
  class EmergencyModeTracker:
    def __init__(self, history_dir: Path, program_name: str, *, time_threshold: float, attempt_threshold: int) -> None: ...
    @property
    def writer(self) -> EmergencyHistoryWriter | None: ...
    def record_failure(self) -> None: ...
    def record_success(self) -> None: ...
    def maybe_enter(self) -> None: ...
    def close(self) -> None: ...
  ```
  `time_threshold` is already-in-seconds (callers pass `emergency_time_threshold * 60.0`, matching today's
  constructors).

- [ ] **Step 1: Write the failing tests**

Create `tests/central_log_server/test_client_emergency.py`:

```python
"""Tests for `aeth_ext.central_log_server.client.emergency.EmergencyModeTracker`."""

# Standard library imports
from time import monotonic
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.client.emergency import EmergencyModeTracker

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


class TestMaybeEnter:
  def test_enters_emergency_mode_once_both_thresholds_are_exceeded(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    tracker.maybe_enter()

    assert tracker.writer is not None
    tracker.close()

  def test_stays_out_of_emergency_mode_below_thresholds(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=999.0, attempt_threshold=999)
    tracker.record_failure()

    tracker.maybe_enter()

    assert tracker.writer is None

  def test_does_not_spin_up_a_second_writer_once_already_active(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    first_writer = tracker.writer

    tracker.maybe_enter()

    assert tracker.writer is first_writer
    tracker.close()


class TestRecordFailure:
  def test_increments_consecutive_failures_towards_the_attempt_threshold(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=2)
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    tracker.record_failure()
    tracker.maybe_enter()
    assert tracker.writer is None

    tracker.record_failure()
    tracker.maybe_enter()
    assert tracker.writer is not None
    tracker.close()


class TestRecordSuccess:
  def test_closes_the_writer_and_resets_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    writer = tracker.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    tracker.record_success()

    assert tracker.writer is None
    assert writer_closed == [True]
    assert tracker._consecutive_failures == 0  # pyright: ignore[reportPrivateUsage]

  def test_is_a_noop_when_not_in_emergency_mode(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)

    tracker.record_success()  # must not raise

    assert tracker.writer is None


class TestClose:
  def test_closes_an_active_writer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)
    tracker.record_failure()
    tracker._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    tracker.maybe_enter()
    writer = tracker.writer
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    tracker.close()

    assert tracker.writer is None
    assert writer_closed == [True]

  def test_is_a_noop_when_not_in_emergency_mode(self, tmp_path: Path):
    tracker = EmergencyModeTracker(tmp_path, "prog", time_threshold=0.0, attempt_threshold=1)

    tracker.close()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail on import**

Run: `uv run pytest tests/central_log_server/test_client_emergency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aeth_ext.central_log_server.client.emergency'`

- [ ] **Step 3: Implement `EmergencyModeTracker`**

Create `src/aeth_ext/central_log_server/client/emergency.py`:

```python
# Standard library imports
import logging
from time import monotonic
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["EmergencyModeTracker"]


class EmergencyModeTracker:
  """Tracks whether a client transport should be writing straight to its emergency history file.

  Once both *time_threshold* (seconds since the last successful send) and *attempt_threshold*
  (consecutive failed attempts) are exceeded, :meth:`maybe_enter` spins up an
  :class:`~aeth_ext.central_log_server.client.history.EmergencyHistoryWriter` so new records survive a
  process crash even while the server is unreachable. Deliberately decoupled from any
  :class:`~aeth_ext.central_log_server.client.durability.RecordDurability` instance - it only needs a
  directory and a program name, not a whole durability object, so it can be constructed and tested
  independently.
  """

  def __init__(self, history_dir: Path, program_name: str, *, time_threshold: float, attempt_threshold: int) -> None:
    self._history_dir = history_dir
    self._program_name = program_name
    self._time_threshold = time_threshold
    self._attempt_threshold = attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._writer: EmergencyHistoryWriter | None = None

  @property
  def writer(self) -> EmergencyHistoryWriter | None:
    """The active emergency writer, or ``None`` if not currently in emergency mode."""
    return self._writer

  def record_failure(self) -> None:
    """Count one more consecutive failed send/connect attempt."""
    self._consecutive_failures += 1

  def record_success(self) -> None:
    """Reset the failure counter and stamp the last-success time; exit emergency mode if it was active."""
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    writer = self._writer
    if writer is None:
      return
    self._writer = None
    writer.close()
    logger.info("Log server reachable again for %r; stopped emergency history writer", self._program_name)

  def maybe_enter(self) -> None:
    """Spin up an emergency writer if both thresholds have tripped and one isn't already active."""
    if self._writer is not None:
      return
    elapsed = monotonic() - self._last_success_monotonic
    if elapsed >= self._time_threshold and self._consecutive_failures >= self._attempt_threshold:
      self._writer = EmergencyHistoryWriter(self._history_dir)
      logger.warning(
        "Log server unreachable for %.0fs after %d attempts for %r; writing new records directly to history file",
        elapsed,
        self._consecutive_failures,
        self._program_name,
      )

  def close(self) -> None:
    """Close the active writer, if any. Idempotent."""
    if self._writer is not None:
      self._writer.close()
      self._writer = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/central_log_server/test_client_emergency.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/emergency.py tests/central_log_server/test_client_emergency.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/emergency.py tests/central_log_server/test_client_emergency.py`
Expected: `0 errors, 0 warnings, 0 informations`

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/central_log_server/client/emergency.py tests/central_log_server/test_client_emergency.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): extract EmergencyModeTracker component

First step of deduplicating the three client transport classes'
identical emergency-mode state machine (PR review comment on
client/__init__.py). Adds the standalone component and its own test
suite; the three transport classes are wired to use it in later
commits.
EOF
)"
```

---

## Task 2: `RecordDurability` component

**Files:**
- Create: `src/aeth_ext/central_log_server/client/durability.py`
- Test: `tests/central_log_server/test_client_durability.py`

**Interfaces:**
- Consumes: `aeth_ext.central_log_server.client.history.RecordHistoryBuffer` (existing,
  `RecordHistoryBuffer(program_name, max_records, max_bytes, max_age)`), `HistoryEntry`,
  `aeth_ext.central_log_server.client.id_checkpoint.{IdCheckpointBackend,ThreadedIdCheckpointBackend,AsyncioIdCheckpointBackend}`
  (existing, unchanged), `aeth_ext.central_log_server.client.history.EmergencyHistoryWriter` (only as a
  type for the `record()` parameter), `aeth_ext.central_log_server.protocol.HandshakeAck`,
  `aeth_ext.logging.bases.TaggedLogRecord`, `aeth_ext.settings.BaseSettings`.
- Produces (used by Tasks 4-6):
  ```python
  class RecordDurability:
    def __init__(
      self, program_name: str, max_records: int = 50_000, max_bytes: int = 64 * 1024 * 1024, max_age: float = 300.0,
      *, id_checkpoint_backend: Literal["thread", "asyncio"] = "thread", event_loop: AbstractEventLoop | None = None,
    ) -> None: ...
    history: RecordHistoryBuffer                    # public, for direct test access matching existing convention
    @property
    def history_dir(self) -> Path: ...
    @property
    def last_sent_id(self) -> int: ...
    def record(self, record: TaggedLogRecord, *, local_root: logging.Logger | None, emergency_writer: EmergencyHistoryWriter | None) -> HistoryEntry: ...
    def mark_sent(self, entry_id: int) -> None: ...
    def resolve_backlog(self, ack: HandshakeAck) -> tuple[HistoryEntry, ...]: ...
    def arm_shutdown(self) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
  ```

- [ ] **Step 1: Write the failing tests**

Create `tests/central_log_server/test_client_durability.py`:

```python
"""Tests for `aeth_ext.central_log_server.client.durability.RecordDurability`."""

# Standard library imports
import logging
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.client import durability as durability_module
from aeth_ext.central_log_server.client.durability import RecordDurability
from aeth_ext.central_log_server.client.history import RecordHistoryBuffer
from aeth_ext.central_log_server.protocol import HandshakeAck
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path


def _make_record(message: str = "hello") -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", logging.INFO, __file__, 1, message, None, None)


class _FakeLocalRoot:
  def __init__(self) -> None:
    self.handled: list[TaggedLogRecord] = []

  def handle(self, record: TaggedLogRecord) -> None:
    self.handled.append(record)


class _FakeEmergencyWriter:
  def __init__(self) -> None:
    self.submitted: list[Any] = []

  def submit(self, entry: Any) -> None:
    self.submitted.append(entry)


@pytest.fixture
def make_durability(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
  """Build RecordDurability instances whose disk side effects stay inside tmp_path, closing them on teardown."""
  persist_dir = tmp_path / "persist"
  persist_dir.mkdir()
  monkeypatch.setattr(durability_module.settings, "persisted_dir_loc", persist_dir)
  monkeypatch.setattr(RecordHistoryBuffer, "history_root", tmp_path / "hist")
  created: list[RecordDurability] = []

  def factory(**kwargs: Any) -> RecordDurability:
    durability = RecordDurability("prog", **kwargs)
    created.append(durability)
    return durability

  yield factory

  for durability in created:
    durability.close()


class TestRecord:
  def test_first_record_gets_id_one_and_advances_next_id(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    first = durability.record(_make_record(), local_root=None, emergency_writer=None)
    second = durability.record(_make_record(), local_root=None, emergency_writer=None)

    assert first.id == 1
    assert second.id == 2
    assert first.record.record_id == 1
    assert second.record.record_id == 2

  def test_record_is_appended_to_history(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    entry = durability.record(_make_record(), local_root=None, emergency_writer=None)

    assert durability.history.find_after(None, None) == (entry,)

  def test_local_root_receives_the_record_when_provided(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()
    local_root = _FakeLocalRoot()
    record = _make_record()

    durability.record(record, local_root=local_root, emergency_writer=None)  # pyright: ignore[reportArgumentType]

    assert local_root.handled == [record]

  def test_local_root_is_not_touched_when_none(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    entry = durability.record(_make_record(), local_root=None, emergency_writer=None)  # must not raise

    assert entry.persisted is False

  def test_emergency_writer_receives_the_entry_and_it_is_marked_persisted(
    self, make_durability: Callable[..., RecordDurability]
  ):
    durability = make_durability()
    writer = _FakeEmergencyWriter()

    entry = durability.record(_make_record(), local_root=None, emergency_writer=writer)  # pyright: ignore[reportArgumentType]

    assert writer.submitted == [entry]
    assert entry.persisted is True

  def test_entry_is_not_marked_persisted_when_no_emergency_writer(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    entry = durability.record(_make_record(), local_root=None, emergency_writer=None)

    assert entry.persisted is False


class TestMarkSentAndLastSentId:
  def test_defaults_to_zero(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    assert durability.last_sent_id == 0

  def test_mark_sent_updates_last_sent_id(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    durability.mark_sent(5)

    assert durability.last_sent_id == 5


class TestResolveBacklog:
  def test_returns_entries_after_the_acked_id(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()
    for _ in range(3):
      durability.record(_make_record(), local_root=None, emergency_writer=None)

    result = durability.resolve_backlog(HandshakeAck(ok=True, last_record_id=1))

    assert [entry.id for entry in result] == [2, 3]

  def test_returns_empty_tuple_and_logs_when_the_acked_id_is_unrecoverable(
    self, make_durability: Callable[..., RecordDurability], caplog: pytest.LogCaptureFixture
  ):
    durability = make_durability(max_records=1)
    durability.record(_make_record(), local_root=None, emergency_writer=None)  # flushes id 1 to disk

    with caplog.at_level(logging.WARNING):
      result = durability.resolve_backlog(HandshakeAck(ok=True, last_record_id=999, last_received_at=1.0))

    assert result == ()
    assert "could not be located in history" in caplog.text


class TestArmShutdownFlushClose:
  def test_arm_shutdown_switches_history_to_write_through(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    durability.arm_shutdown()
    durability.record(_make_record(), local_root=None, emergency_writer=None)

    assert list(durability.history_dir.glob("*.jsonl"))

  def test_flush_flushes_the_history_buffer(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()
    durability.record(_make_record(), local_root=None, emergency_writer=None)

    durability.flush()

    assert list(durability.history_dir.glob("*.jsonl"))

  def test_close_closes_the_id_checkpoint(self, make_durability: Callable[..., RecordDurability]):
    durability = make_durability()

    durability.close()

    assert not durability._id_checkpoint._thread.is_alive()  # pyright: ignore[reportPrivateUsage]
```

- [ ] **Step 2: Run tests to verify they fail on import**

Run: `uv run pytest tests/central_log_server/test_client_durability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aeth_ext.central_log_server.client.durability'`

- [ ] **Step 3: Implement `RecordDurability`**

Create `src/aeth_ext/central_log_server/client/durability.py`:

```python
# Standard library imports
import logging
from typing import TYPE_CHECKING, Any, Literal

# First party imports
from aeth_ext.central_log_server.client.history import EmergencyHistoryWriter, HistoryEntry, RecordHistoryBuffer
from aeth_ext.central_log_server.client.id_checkpoint import (
  AsyncioIdCheckpointBackend,
  IdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from asyncio import AbstractEventLoop
  from pathlib import Path

  # First party imports
  from aeth_ext.central_log_server.protocol import HandshakeAck
  from aeth_ext.logging.bases import TaggedLogRecord

logger = logging.getLogger(__name__)

settings = BaseSettings.get_settings()

__all__ = ["RecordDurability"]


class RecordDurability:
  """Owns everything about making one client transport's records durable and replayable.

  Wraps a :class:`~aeth_ext.central_log_server.client.history.RecordHistoryBuffer` and an
  :class:`~aeth_ext.central_log_server.client.id_checkpoint.IdCheckpointBackend`, and provides the
  "assign an id, wrap in a HistoryEntry, append to history, schedule checkpoint persistence, optionally
  hand to a local logger, optionally submit to an active emergency writer" sequence as a single
  :meth:`record` call - this exact sequence was previously duplicated 5 times across the three client
  transport classes.
  """

  def __init__(
    self,
    program_name: str,
    max_records: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_age: float = 300.0,
    *,
    id_checkpoint_backend: Literal["thread", "asyncio"] = "thread",
    event_loop: AbstractEventLoop | None = None,
  ) -> None:
    self._program_name = program_name
    self.history = RecordHistoryBuffer(program_name, max_records, max_bytes, max_age)

    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend
    if id_checkpoint_backend == "asyncio":
      if event_loop is None:
        raise ValueError("event_loop is required when id_checkpoint_backend='asyncio'")
      self._id_checkpoint = AsyncioIdCheckpointBackend(checkpoint_path, event_loop)
    else:
      self._id_checkpoint = ThreadedIdCheckpointBackend(checkpoint_path)

    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

  @property
  def history_dir(self) -> Path:
    return self.history.history_dir

  @property
  def last_sent_id(self) -> int:
    return self._last_sent_id

  def record(
    self, record: TaggedLogRecord, *, local_root: logging.Logger | None, emergency_writer: EmergencyHistoryWriter | None
  ) -> HistoryEntry:
    """Assign a durable id to *record* and persist it. Returns the entry for the caller to transmit."""
    record_id = self._next_id
    self._next_id += 1
    record.record_id = record_id
    entry = HistoryEntry(id=record_id, created=record.created, record=record)
    self.history.append(entry)
    self._id_checkpoint.schedule_persist(record_id)
    if local_root is not None:
      local_root.handle(record)
    if emergency_writer is not None:
      entry.persisted = True
      emergency_writer.submit(entry)
    return entry

  def mark_sent(self, entry_id: int) -> None:
    """Record that *entry_id* has been confirmed on the wire."""
    self._last_sent_id = entry_id

  def resolve_backlog(self, ack: HandshakeAck) -> tuple[HistoryEntry, ...]:
    """Whatever *ack* says the server is missing, in order; ``()`` if there's nothing to replay.

    An unrecoverable gap (the acked id can't be located in memory or on disk) also returns ``()`` after
    logging a warning - the caller resumes live either way.
    """
    backlog = self.history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return ()
    return backlog

  def arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch history and checkpoint to write-through. Atomic stores only."""
    self.history.begin_shutdown()
    self._id_checkpoint.begin_shutdown()

  def flush(self) -> None:
    """Force every buffered history entry to disk, ignoring the usual thresholds."""
    self.history.flush()

  def close(self) -> None:
    """Stop the id-checkpoint backend's background work, flushing its most recent id first."""
    self._id_checkpoint.close()
```

Note: `TaggedLogRecord` stays `TYPE_CHECKING`-only per this project's annotation rule (a plain method
parameter annotation, not a pydantic dataclass field — see `.claude/CLAUDE.md`'s Annotation Conventions).
`local_root.handle(record)` takes a `# pyright: ignore[reportArgumentType]` because `logging.Logger.handle`
is typed for `logging.LogRecord`, matching the `# type: ignore[union-attr]`-style suppressions already
present at the original call sites for the same stdlib-vs-`TaggedLogRecord` mismatch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/central_log_server/test_client_durability.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/durability.py tests/central_log_server/test_client_durability.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/durability.py tests/central_log_server/test_client_durability.py`
Expected: `0 errors, 0 warnings, 0 informations`

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/central_log_server/client/durability.py tests/central_log_server/test_client_durability.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): extract RecordDurability component

Second step of deduplicating the three client transport classes (PR
review comment on client/__init__.py). Wraps RecordHistoryBuffer and
IdCheckpointBackend together with the record-bookkeeping sequence and
backlog-resolution logic that were previously duplicated 5x and 3x
respectively. The three transport classes are wired to use it in
later commits.
EOF
)"
```

---

## Task 3: Extract `read_server_message_sync`/`read_server_message_async` free functions

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py` (add two functions near `_recv_exact`;
  do not yet touch the three classes' own `_read_message` methods or call sites — that happens in Tasks 4-6)
- Modify: `tests/central_log_server/test_client.py` (migrate `TestReadMessage` to call the new free
  function directly instead of `handler._read_message`; add a new async test class)

**Interfaces:**
- Produces (used by Tasks 4-6):
  ```python
  def read_server_message_sync(sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None: ...
  async def read_server_message_async(reader: asyncio.StreamReader, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None: ...
  ```

- [ ] **Step 1: Write the failing/updated tests**

In `tests/central_log_server/test_client.py`, replace the `TestReadMessage` class (currently around line
135) with the following two classes. This drops the `make_handler` fixture dependency entirely for these
tests since the functions no longer need a handler instance:

```python
class TestReadServerMessageSync:
  def test_parses_valid_ack(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))

      message = client_mod.read_server_message_sync(client_side)

      assert isinstance(message, HandshakeAck)
      assert message.ok is True
      assert message.last_record_id == _ACK_LAST_ID
    finally:
      client_side.close()
      server_side.close()

  def test_parses_apply_failure(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(encode_json_packet(ApplyFailure(error="disk full")))

      message = client_mod.read_server_message_sync(client_side)

      assert isinstance(message, ApplyFailure)
      assert message.error == "disk full"
    finally:
      client_side.close()
      server_side.close()

  @pytest.mark.parametrize(
    "payload",
    [
      pytest.param(b"garbage", id="malformed json"),
      pytest.param(b"[1, 2]", id="non-dict"),
      pytest.param(b'{"nonsense": true}', id="wrong keys"),
    ],
  )
  def test_malformed_payload_returns_none(self, payload: bytes):
    client_side, server_side = socket.socketpair()
    try:
      server_side.sendall(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()
      server_side.close()

  def test_peer_hangup_returns_none(self):
    client_side, server_side = socket.socketpair()
    try:
      server_side.close()

      assert client_mod.read_server_message_sync(client_side) is None
    finally:
      client_side.close()


class TestReadServerMessageAsync:
  async def test_parses_valid_ack(self):
    reader = asyncio.StreamReader()
    reader.feed_data(encode_json_packet(HandshakeAck(ok=True, last_record_id=_ACK_LAST_ID)))
    reader.feed_eof()

    message = await client_mod.read_server_message_async(reader)

    assert isinstance(message, HandshakeAck)
    assert message.ok is True
    assert message.last_record_id == _ACK_LAST_ID

  async def test_parses_apply_failure(self):
    reader = asyncio.StreamReader()
    reader.feed_data(encode_json_packet(ApplyFailure(error="disk full")))
    reader.feed_eof()

    message = await client_mod.read_server_message_async(reader)

    assert isinstance(message, ApplyFailure)
    assert message.error == "disk full"

  @pytest.mark.parametrize(
    "payload",
    [
      pytest.param(b"garbage", id="malformed json"),
      pytest.param(b"[1, 2]", id="non-dict"),
      pytest.param(b'{"nonsense": true}', id="wrong keys"),
    ],
  )
  async def test_malformed_payload_returns_none(self, payload: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(client_mod.LENGTH_STRUCT.pack(len(payload)) + payload)
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None

  async def test_peer_hangup_returns_none(self):
    reader = asyncio.StreamReader()
    reader.feed_eof()

    assert await client_mod.read_server_message_async(reader) is None
```

Add `import asyncio` to the top of `tests/central_log_server/test_client.py`'s standard-library import
block if not already present (it is not, today).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/central_log_server/test_client.py -k "TestReadServerMessage" -v`
Expected: FAIL — `AttributeError: module 'aeth_ext.central_log_server.client' has no attribute 'read_server_message_sync'`

- [ ] **Step 3: Add the free functions**

In `src/aeth_ext/central_log_server/client/__init__.py`, add these two functions directly after the
existing `_recv_exact` function (do not remove `_recv_exact` - both new functions still use it):

```python
def read_server_message_sync(sock: socket.socket, timeout: float | None = 5.0) -> HandshakeAck | ApplyFailure | None:
  """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

  A failure here (timeout, malformed payload, or the connection dying) is not fatal to the connection
  itself - the caller decides what that means for its own purposes - so the socket is left untouched on
  any error; a genuinely broken socket will surface the next time a record is actually sent.
  ``ApplySuccess`` is deliberately not in the return type: nothing reads its fields, callers only ever
  need to check for ``ApplyFailure``.

  Restoring the previous timeout is itself wrapped in ``suppress(OSError)``: when called from a
  background watcher thread, *sock* can be closed concurrently by whichever thread owns it (e.g. a send
  failure or ``close()``), racing this cleanup step after ``recv`` already returned.
  """
  previous_timeout = sock.gettimeout()
  sock.settimeout(timeout)
  try:
    header = _recv_exact(sock, LENGTH_STRUCT.size)
    if header is None:
      return None
    (length,) = LENGTH_STRUCT.unpack(header)
    payload = _recv_exact(sock, length)
    if payload is None:
      return None
    message = decode_server_message(payload)
    return message if isinstance(message, HandshakeAck | ApplyFailure) else None
  except OSError:
    return None
  finally:
    with suppress(OSError):
      sock.settimeout(previous_timeout)


async def read_server_message_async(
  reader: asyncio.StreamReader,
  timeout: float | None = 5.0,  # noqa: ASYNC109
) -> HandshakeAck | ApplyFailure | None:
  """Best-effort read of one server message (D-E5), or ``None`` if malformed/absent/timed out.

  ``ApplySuccess`` is deliberately not in the return type: nothing reads its fields, callers only ever
  need to check for ``ApplyFailure``.
  """
  try:
    async with asyncio.timeout(timeout):
      header = await reader.readexactly(LENGTH_STRUCT.size)
      (length,) = LENGTH_STRUCT.unpack(header)
      payload = await reader.readexactly(length)
    message = decode_server_message(payload)
    return message if isinstance(message, HandshakeAck | ApplyFailure) else None
  except OSError, TimeoutError, asyncio.IncompleteReadError:
    return None
```

Do not remove the three classes' own `_read_message` methods or their call sites yet — those are removed
in Tasks 4, 5, and 6 respectively, alongside wiring each class to the new components.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/central_log_server/test_client.py -k "TestReadServerMessage" -v`
Expected: PASS (8 tests: 4 sync + 4 async)

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py`
Expected: pre-existing unrelated errors only (this file has some already, e.g. `TaggedLogRecord`/`LogRecord`
handler-override mismatches noted during the earlier review-comment fixes on this branch); no *new* errors
introduced by this step.

- [ ] **Step 6: Run the full existing client test file to confirm no regressions**

Run: `uv run pytest tests/central_log_server/test_client.py -v`
Expected: PASS (all tests, including the ones not touched by this task)

- [ ] **Step 7: Commit**

```bash
git add src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): extract read_server_message_sync/async free functions

Two of the three client transport classes' _read_message methods were
already byte-for-byte identical, and none of the three reference
`self` in their bodies - promoting them to module-level functions
removes the duplication without needing a stateful component. The
three transport classes still call their own _read_message methods
for now; they're wired to the free functions in the following commits
alongside removing those duplicate methods.
EOF
)"
```

---

## Task 4: Wire `HandshakeSocketHandler` to the new components

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py:93-487` (the `HandshakeSocketHandler` class)
- Modify: `tests/central_log_server/test_client.py`

**Interfaces:**
- Consumes: `RecordDurability` and `EmergencyModeTracker` from Tasks 1-2, `read_server_message_sync` from
  Task 3.

- [ ] **Step 1: Add the new imports**

In `src/aeth_ext/central_log_server/client/__init__.py`, replace the existing history/id_checkpoint import
block:

```python
from aeth_ext.central_log_server.client.history import (
  EmergencyHistoryWriter,
  HistoryEntry,
  RecordHistoryBuffer,
)
from aeth_ext.central_log_server.client.id_checkpoint import (
  AsyncioIdCheckpointBackend,
  IdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)
```

with:

```python
from aeth_ext.central_log_server.client.durability import RecordDurability
from aeth_ext.central_log_server.client.emergency import EmergencyModeTracker
from aeth_ext.central_log_server.client.history import HistoryEntry
```

(`RecordHistoryBuffer`, `EmergencyHistoryWriter`, and the three `id_checkpoint` backends are no longer
referenced directly in this file once all three classes are wired - Task 7 double-checks this, but adding
the correct imports now avoids a broken intermediate state for `HandshakeSocketHandler` specifically.
`AsyncioQueueDrainer`/`ThreadedQueueDrainer` still reference `RecordHistoryBuffer`/`EmergencyHistoryWriter`/the
id_checkpoint backends directly until Tasks 5-6, so ruff will flag those as still-used until then - don't
worry about unused-import warnings until Task 7.)

- [ ] **Step 2: Rewrite the constructor and `_arm_shutdown`**

Replace (inside `HandshakeSocketHandler.__init__`, from `self._history = RecordHistoryBuffer(...)` through
the end of `_arm_shutdown`):

```python
    self._history = RecordHistoryBuffer(self._program_name, max_history_records, max_history_bytes, max_history_age)

    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend
    if id_checkpoint_backend == "asyncio":
      if event_loop is None:
        raise ValueError("event_loop is required when id_checkpoint_backend='asyncio'")
      self._id_checkpoint = AsyncioIdCheckpointBackend(checkpoint_path, event_loop)
    else:
      self._id_checkpoint = ThreadedIdCheckpointBackend(checkpoint_path)

    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

    self._emergency_time_threshold = emergency_time_threshold * 60.0
    self._emergency_attempt_threshold = emergency_attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._emergency_writer: EmergencyHistoryWriter | None = None

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(self.close, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY)

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._history.begin_shutdown()
    self._id_checkpoint.begin_shutdown()
```

with:

```python
    self._durability = RecordDurability(
      self._program_name,
      max_history_records,
      max_history_bytes,
      max_history_age,
      id_checkpoint_backend=id_checkpoint_backend,
      event_loop=event_loop,
    )
    self._emergency = EmergencyModeTracker(
      self._durability.history_dir,
      self._program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(self.close, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY)

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._durability.arm_shutdown()
```

- [ ] **Step 3: Update `_send_handshake` and `_watch_apply_result` to use the free function**

Replace `message = self._read_message(sock)` (in `_send_handshake`) with:
```python
    message = read_server_message_sync(sock)
```

Replace `message = self._read_message(sock, timeout=None)` (in `_watch_apply_result`) with:
```python
    message = read_server_message_sync(sock, timeout=None)
```

- [ ] **Step 4: Delete the class's own `_read_message` method**

Delete the entire `_read_message` method from `HandshakeSocketHandler` (it duplicated
`read_server_message_sync`, added in Task 3).

- [ ] **Step 5: Rewrite `_replay_backlog`**

Replace:

```python
  def _replay_backlog(self, ack: HandshakeAck) -> None:
    """Resend whatever the server's ack says it is missing, in order.

    If the id the server last confirmed can't be located in memory or on
    disk, the gap is logged and, per plan, the client also resumes live
    rather than blocking.
    """
    backlog = self._history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return
    sock = self.sock
    if sock is None:
      return
    for entry in backlog:
      try:
        sock.sendall(self.makePickle(entry.record))
      except OSError:
        sock.close()
        self.sock = None
        return
      self._last_sent_id = entry.id
```

with:

```python
  def _replay_backlog(self, ack: HandshakeAck) -> None:
    """Resend whatever the server's ack says it is missing, in order."""
    sock = self.sock
    if sock is None:
      return
    for entry in self._durability.resolve_backlog(ack):
      try:
        sock.sendall(self.makePickle(entry.record))
      except OSError:
        sock.close()
        self.sock = None
        return
      self._durability.mark_sent(entry.id)
```

- [ ] **Step 6: Rewrite `emit`**

Replace the body from `record_id = self._next_id` through the end of the method:

```python
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)

      if self._emergency_writer is not None:
        entry.persisted = True
        self._emergency_writer.submit(entry)

      if self._transmit(entry):
        self._consecutive_failures = 0
        self._last_success_monotonic = monotonic()
        if self._emergency_writer is not None:
          self._exit_emergency_mode()
      else:
        self._consecutive_failures += 1
        self._maybe_enter_emergency_mode()
```

with:

```python
      entry = self._durability.record(record, local_root=None, emergency_writer=self._emergency.writer)

      if self._transmit(entry):
        self._emergency.record_success()
      else:
        self._emergency.record_failure()
        self._emergency.maybe_enter()
```

- [ ] **Step 7: Rewrite `_transmit`**

Replace `if entry.id <= self._last_sent_id:` with `if entry.id <= self._durability.last_sent_id:`, and
replace `self._last_sent_id = entry.id` (at the end of the method) with `self._durability.mark_sent(entry.id)`.

- [ ] **Step 8: Delete `_maybe_enter_emergency_mode` and `_exit_emergency_mode`**

Delete both methods entirely from `HandshakeSocketHandler` (their logic now lives in `EmergencyModeTracker`).

- [ ] **Step 9: Rewrite `close`**

Replace:

```python
  @override
  def close(self) -> None:
    """Flush history, then tear down the checkpoint, emergency writer, and socket.

    Reachable both from ``logging.shutdown``'s ``atexit`` pass and from the
    shutdown registry, so every step here has to tolerate running twice. The
    history flush is new: previously nothing flushed it on any path, so whatever
    sat in memory below the spill thresholds was simply lost.
    """
    self._history.flush()
    self._id_checkpoint.close()
    if self._emergency_writer is not None:
      self._emergency_writer.close()
      self._emergency_writer = None
    super().close()
```

with:

```python
  @override
  def close(self) -> None:
    """Flush history, then tear down the checkpoint, emergency writer, and socket.

    Reachable both from ``logging.shutdown``'s ``atexit`` pass and from the
    shutdown registry, so every step here has to tolerate running twice. The
    history flush is new: previously nothing flushed it on any path, so whatever
    sat in memory below the spill thresholds was simply lost.
    """
    self._durability.flush()
    self._durability.close()
    self._emergency.close()
    super().close()
```

- [ ] **Step 10: Update `tests/central_log_server/test_client.py`'s tests that reach into the moved private attributes**

In `TestEmitPreFilter`:
- `test_undeliverable_record_never_consumes_an_id`: change
  `monkeypatch.setattr(handler._history, "append", appended.append)` to
  `monkeypatch.setattr(handler._durability.history, "append", appended.append)`, and change
  `assert handler._next_id == _FIRST_ID` to `assert handler._durability._next_id == _FIRST_ID  # pyright: ignore[reportPrivateUsage]`.
- `test_deliverable_record_gets_id_history_and_transmission`: change
  `assert handler._next_id == _FIRST_ID + 1` to `assert handler._durability._next_id == _FIRST_ID + 1  # pyright: ignore[reportPrivateUsage]`.

In `TestTransmit`:
- `test_fresh_send_delivers_the_record_and_advances_last_sent_id`: change
  `assert handler._last_sent_id == _FIRST_ID` to `assert handler._durability.last_sent_id == _FIRST_ID`.
- `test_an_entry_at_or_before_the_last_sent_id_is_a_noop`: change
  `handler._last_sent_id = _ACK_LAST_ID` to `handler._durability.mark_sent(_ACK_LAST_ID)`, and change
  `assert handler._last_sent_id == _ACK_LAST_ID` to `assert handler._durability.last_sent_id == _ACK_LAST_ID`.

Replace the entire `TestEmergencyMode` class with:

```python
class TestEmergencyMode:
  def test_enters_emergency_mode_once_both_thresholds_are_exceeded(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    handler._emergency.record_failure()  # pyright: ignore[reportPrivateUsage]
    handler._emergency._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    handler._emergency.maybe_enter()  # pyright: ignore[reportPrivateUsage]

    assert handler._emergency.writer is not None  # pyright: ignore[reportPrivateUsage]

  def test_stays_out_of_emergency_mode_below_thresholds(self, make_handler: Callable[..., HandshakeSocketHandler]):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=999.0, emergency_attempt_threshold=999)
    handler._emergency.record_failure()  # pyright: ignore[reportPrivateUsage]

    handler._emergency.maybe_enter()  # pyright: ignore[reportPrivateUsage]

    assert handler._emergency.writer is None  # pyright: ignore[reportPrivateUsage]

  def test_record_success_closes_the_writer_and_resets_failures(
    self, make_handler: Callable[..., HandshakeSocketHandler], monkeypatch: pytest.MonkeyPatch
  ):
    handler = make_handler(_REACHABLE_CONFIG, emergency_time_threshold=0.0, emergency_attempt_threshold=1)
    handler._emergency.record_failure()  # pyright: ignore[reportPrivateUsage]
    handler._emergency._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]
    handler._emergency.maybe_enter()  # pyright: ignore[reportPrivateUsage]
    writer = handler._emergency.writer  # pyright: ignore[reportPrivateUsage]
    assert writer is not None
    writer_closed: list[bool] = []
    monkeypatch.setattr(writer, "close", lambda: writer_closed.append(True))

    handler._emergency.record_success()  # pyright: ignore[reportPrivateUsage]

    assert handler._emergency.writer is None  # pyright: ignore[reportPrivateUsage]
    assert handler._emergency._consecutive_failures == 0  # pyright: ignore[reportPrivateUsage]
    assert writer_closed == [True]
```

In `TestClose.test_close_closes_id_checkpoint_and_any_active_emergency_writer`:
- change `monkeypatch.setattr(handler._id_checkpoint, "close", lambda: checkpoint_closed.append(True))` to
  `monkeypatch.setattr(handler._durability._id_checkpoint, "close", lambda: checkpoint_closed.append(True))  # pyright: ignore[reportPrivateUsage]`
- change every `handler._consecutive_failures`/`handler._last_success_monotonic`/`handler._maybe_enter_emergency_mode`/`handler._emergency_writer`
  reference to the `handler._emergency.*` equivalents shown in the rewritten `TestEmergencyMode` class above.

- [ ] **Step 11: Run the full test file**

Run: `uv run pytest tests/central_log_server/test_client.py -v`
Expected: PASS (all tests)

- [ ] **Step 12: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py`
Expected: no new errors versus the baseline noted in Task 3 Step 5.

- [ ] **Step 13: Commit**

```bash
git add src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): wire HandshakeSocketHandler to RecordDurability/EmergencyModeTracker

Third step of deduplicating the three client transport classes (PR
review comment on client/__init__.py). Replaces this class's own
copies of the record-bookkeeping sequence, emergency-mode state
machine, backlog-replay logic, and _read_message with calls into the
new RecordDurability/EmergencyModeTracker components and the
read_server_message_sync free function. No behavior change other than
the two deltas documented in the design spec (program name now in the
emergency-enter log message; harmless side-effect reordering that
does not apply to this class).
EOF
)"
```

---

## Task 5: Wire `AsyncioQueueDrainer` to the new components

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py` (the `AsyncioQueueDrainer` class)
- Modify: `tests/central_log_server/test_client_queue_drainers.py`

**Interfaces:**
- Consumes: `RecordDurability`, `EmergencyModeTracker` (Tasks 1-2, already imported in this file from
  Task 4), `read_server_message_async` (Task 3).

- [ ] **Step 1: Rewrite the constructor and `_arm_shutdown`**

Replace (inside `AsyncioQueueDrainer.__init__`, from `self._history = RecordHistoryBuffer(...)` through
the end of `_arm_shutdown`):

```python
    self._history = RecordHistoryBuffer(self._program_name, max_history_records, max_history_bytes, max_history_age)
    checkpoint_path = settings.persisted_dir_loc / "logging_ids.checkpoint"
    self._id_checkpoint: IdCheckpointBackend = ThreadedIdCheckpointBackend(checkpoint_path)
    self._next_id = self._id_checkpoint.load() + 1
    self._last_sent_id = 0

    self._emergency_time_threshold = emergency_time_threshold * 60.0
    self._emergency_attempt_threshold = emergency_attempt_threshold
    self._consecutive_failures = 0
    self._last_success_monotonic = monotonic()
    self._emergency_writer: EmergencyHistoryWriter | None = None

    self._local_manager: logging.Manager | None = None
    self._local_root: logging.Logger | None = None
    if log_locally or _cfg["testing"]:
      # First party imports
      from aeth_ext.logging.config import DictConfigurator

      self._local_manager, self._local_root = DictConfigurator(_cfg["local"], log_dir=settings.log_loc_folder).apply(private=True)

    self._stop_event = asyncio.Event()
    self._task: asyncio.Task[None] | None = None

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._history.begin_shutdown()
    self._id_checkpoint.begin_shutdown()
```

with:

```python
    self._durability = RecordDurability(self._program_name, max_history_records, max_history_bytes, max_history_age)
    self._emergency = EmergencyModeTracker(
      self._durability.history_dir,
      self._program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )

    self._local_manager: logging.Manager | None = None
    self._local_root: logging.Logger | None = None
    if log_locally or _cfg["testing"]:
      # First party imports
      from aeth_ext.logging.config import DictConfigurator

      self._local_manager, self._local_root = DictConfigurator(_cfg["local"], log_dir=settings.log_loc_folder).apply(private=True)

    self._stop_event = asyncio.Event()
    self._task: asyncio.Task[None] | None = None

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._durability.arm_shutdown()
```

- [ ] **Step 2: Rewrite `_finish_shutdown`**

Replace `self._history.flush()` (the first line of the method body) with `self._durability.flush()`.

- [ ] **Step 3: Rewrite `run`'s failure-path emergency calls**

In `run`, there are three places that do `self._consecutive_failures += 1` followed by
`self._maybe_enter_emergency_mode()` (the `OSError` branch after `open_connection`, the `OSError` branch
after `writer.drain()`, and the branch after `_replay_backlog` returns `False`). Replace each occurrence of:

```python
        self._consecutive_failures += 1
        self._maybe_enter_emergency_mode()
```

with:

```python
        self._emergency.record_failure()
        self._emergency.maybe_enter()
```

(three occurrences total in `run`)

- [ ] **Step 4: Update `connect_and_verify` and `_watch_apply_result` to use the free function**

Replace `ack = await self._read_message(reader)` (in `connect_and_verify`) with:
```python
    ack = await read_server_message_async(reader)
```

Replace `message = await self._read_message(reader, timeout=None)` (in `_watch_apply_result`) with:
```python
    message = await read_server_message_async(reader, timeout=None)
```

- [ ] **Step 5: Delete the class's own `_read_message` method**

Delete the entire `_read_message` method from `AsyncioQueueDrainer`.

- [ ] **Step 6: Rewrite `_drain_until_broken`'s bookkeeping block**

Replace:

```python
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      if self._local_root is not None:
        self._local_root.handle(record)
      if self._emergency_writer is not None:
        entry.persisted = True
        self._emergency_writer.submit(entry)
      payload = orjson.dumps(record_to_payload(record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._consecutive_failures = 0
      self._last_success_monotonic = monotonic()
      if self._emergency_writer is not None:
        self._exit_emergency_mode()
```

with:

```python
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      payload = orjson.dumps(record_to_payload(record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._emergency.record_success()
```

- [ ] **Step 7: Rewrite `_replay_backlog`**

Replace:

```python
  async def _replay_backlog(self, ack: HandshakeAck | None, writer: asyncio.StreamWriter) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    backlog = self._history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return True
    for entry in backlog:
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        return False
      self._last_sent_id = entry.id
    return True
```

with:

```python
  async def _replay_backlog(self, ack: HandshakeAck | None, writer: asyncio.StreamWriter) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    for entry in self._durability.resolve_backlog(ack):
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      writer.write(LENGTH_STRUCT.pack(len(payload)) + payload)
      try:
        await writer.drain()
      except OSError:
        return False
      self._durability.mark_sent(entry.id)
    return True
```

- [ ] **Step 8: Rewrite `_sleep_or_drain`'s emergency-loop bookkeeping**

Replace the guard `if self._emergency_writer is None:` (at the top of the method) with
`if self._emergency.writer is None:`.

Replace the loop body:

```python
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      entry.persisted = True
      self._emergency_writer.submit(entry)
      if self._local_root is not None:
        self._local_root.handle(record)
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
```

with:

```python
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
```

- [ ] **Step 9: Delete `_maybe_enter_emergency_mode` and `_exit_emergency_mode`**

Delete both methods entirely from `AsyncioQueueDrainer`.

- [ ] **Step 10: Rewrite `aclose`**

Replace:

```python
    self._history.flush()
    self._stop_event.set()
    if self._task is not None:
      with suppress(asyncio.CancelledError):
        await self._task
      self._task = None
    self._id_checkpoint.close()
    if self._emergency_writer is not None:
      self._emergency_writer.close()
      self._emergency_writer = None
```

with:

```python
    self._durability.flush()
    self._stop_event.set()
    if self._task is not None:
      with suppress(asyncio.CancelledError):
        await self._task
      self._task = None
    self._durability.close()
    self._emergency.close()
```

- [ ] **Step 11: Update the one test that reaches into the moved private attributes**

In `tests/central_log_server/test_client_queue_drainers.py`, replace
`test_enters_emergency_mode_once_both_thresholds_are_exceeded` (in the `AsyncioQueueDrainer` test class)
with:

```python
  async def test_enters_emergency_mode_once_both_thresholds_are_exceeded(
    self, stub_query_logging_configs: dict[str, LoggingConfigResult], isolated_dirs: Path
  ):
    record_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()
    drainer = AsyncioQueueDrainer(
      record_queue, "emergency", host="127.0.0.1", port=1, emergency_time_threshold=0.0, emergency_attempt_threshold=1
    )
    drainer._emergency.record_failure()  # pyright: ignore[reportPrivateUsage]
    drainer._emergency._last_success_monotonic = monotonic() - 1000  # pyright: ignore[reportPrivateUsage]

    drainer._emergency.maybe_enter()  # pyright: ignore[reportPrivateUsage]

    assert drainer._emergency.writer is not None  # pyright: ignore[reportPrivateUsage]
    drainer._emergency.writer.close()  # pyright: ignore[reportPrivateUsage]
```

- [ ] **Step 12: Run the queue-drainer test file's AsyncioQueueDrainer tests**

Run: `uv run pytest tests/central_log_server/test_client_queue_drainers.py -k "TestAsyncioQueueDrainer" -v`
Expected: PASS (all tests in that class)

- [ ] **Step 13: Run the full queue-drainer and server-integration test files**

Run: `uv run pytest tests/central_log_server/test_client_queue_drainers.py tests/central_log_server/test_client_server_integration.py -v`
Expected: PASS (all tests — `ThreadedQueueDrainer` tests in these files still exercise the old,
not-yet-wired code path until Task 6, and must still pass unchanged)

- [ ] **Step 14: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client_queue_drainers.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client_queue_drainers.py`
Expected: no new errors versus the Task 3 baseline.

- [ ] **Step 15: Commit**

```bash
git add src/aeth_ext/central_log_server/client/__init__.py tests/central_log_server/test_client_queue_drainers.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): wire AsyncioQueueDrainer to RecordDurability/EmergencyModeTracker

Fourth step of deduplicating the three client transport classes (PR
review comment on client/__init__.py). Same migration as
HandshakeSocketHandler's: replaces the duplicated record-bookkeeping,
emergency-mode, backlog-replay, and _read_message logic with calls
into the shared components. The emergency-only bulk-drain loop in
_sleep_or_drain now goes through RecordDurability.record(), which
reorders two independent side effects (emergency-submit vs.
local-handle) relative to the old inline code - documented as a
no-op behavioral delta in the design spec.
EOF
)"
```

---

## Task 6: Wire `ThreadedQueueDrainer` to the new components

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py` (the `ThreadedQueueDrainer` class)
- Test: `tests/central_log_server/test_client_queue_drainers.py` (regression only - no test edits
  expected; see Step 10)

**Interfaces:**
- Consumes: `RecordDurability`, `EmergencyModeTracker` (Tasks 1-2), `read_server_message_sync` (Task 3).

- [ ] **Step 1: Rewrite the constructor and `_arm_shutdown`**

Replace (inside `ThreadedQueueDrainer.__init__`, from `self._history = RecordHistoryBuffer(...)` through
the end of `_arm_shutdown`) with the same shape as Task 5 Step 1, adapted for this class's threading-based
fields (`self._stop_event = threading.Event()`, `self._thread = threading.Thread(...)`, which are
unaffected and stay exactly as they are):

```python
    self._durability = RecordDurability(self._program_name, max_history_records, max_history_bytes, max_history_age)
    self._emergency = EmergencyModeTracker(
      self._durability.history_dir,
      self._program_name,
      time_threshold=emergency_time_threshold * 60.0,
      attempt_threshold=emergency_attempt_threshold,
    )

    self._local_manager: logging.Manager | None = None
    self._local_root: logging.Logger | None = None
    if log_locally or _cfg["testing"]:
      # First party imports
      from aeth_ext.logging.config import DictConfigurator

      self._local_manager, self._local_root = DictConfigurator(_cfg["local"], log_dir=settings.log_loc_folder).apply(private=True)

    self._stop_event = threading.Event()
    self._thread = threading.Thread(
      target=self._run,
      name=f"queue-drainer-{self._program_name}",
      daemon=False,
    )

    shutdown.register_for_shutdown(
      self._arm_shutdown, phase=shutdown.ShutdownPhase.INTERRUPT, priority=shutdown.LOGGING_TRANSPORT_PRIORITY, required=True
    )
    shutdown.register_for_shutdown(
      self._finish_shutdown, phase=shutdown.ShutdownPhase.THREADED, priority=shutdown.LOGGING_TRANSPORT_PRIORITY
    )

  def _arm_shutdown(self) -> None:
    """Interrupt-phase arm (D-I8): switch the buffers to write-through.

    Two atomic stores and nothing else -- see
    :attr:`~aeth_ext.errors.ShutdownPhase.INTERRUPT` for the rules this obeys.
    """
    self._durability.arm_shutdown()
```

- [ ] **Step 2: Rewrite `_finish_shutdown` and `stop`**

In `_finish_shutdown`, replace `self._history.flush()` with `self._durability.flush()`.

In `stop`, replace:

```python
    self._history.flush()
    self._stop_event.set()
    self._thread.join(timeout=timeout)
    self._id_checkpoint.close()
    if self._emergency_writer is not None:
      self._emergency_writer.close()
      self._emergency_writer = None
```

with:

```python
    self._durability.flush()
    self._stop_event.set()
    self._thread.join(timeout=timeout)
    self._durability.close()
    self._emergency.close()
```

- [ ] **Step 3: Update `connect_and_verify` and `_connect` to use the free function, and rewrite `_connect`'s emergency call**

Replace `ack = self._read_message(sock)` (in `connect_and_verify`) with:
```python
    ack = read_server_message_sync(sock)
```

In `_connect`, replace `ack = self._read_message(sock)` with `ack = read_server_message_sync(sock)`.

In `_run`, replace:

```python
        self._consecutive_failures += 1
        self._maybe_enter_emergency_mode()
```

with:

```python
        self._emergency.record_failure()
        self._emergency.maybe_enter()
```

- [ ] **Step 4: Delete the class's own `_read_message` method**

Delete the entire `_read_message` method from `ThreadedQueueDrainer`.

- [ ] **Step 5: Rewrite `_check_apply_result`**

Replace `message = self._read_message(sock, timeout=0.5)` with:
```python
    message = read_server_message_sync(sock, timeout=0.5)
```

- [ ] **Step 6: Rewrite `_drain_until_broken`'s bookkeeping block**

Replace:

```python
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      if self._local_root is not None:
        self._local_root.handle(record)
      if self._emergency_writer is not None:
        entry.persisted = True
        self._emergency_writer.submit(entry)
      payload = orjson.dumps(record_to_payload(record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._consecutive_failures = 0
      self._last_success_monotonic = monotonic()
      if self._emergency_writer is not None:
        self._exit_emergency_mode()
```

with:

```python
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      payload = orjson.dumps(record_to_payload(record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        if hasattr(self._queue, "task_done"):
          self._queue.task_done()  # type: ignore[attr-defined]
        return
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
      self._emergency.record_success()
```

- [ ] **Step 7: Rewrite `_replay_backlog`**

Replace:

```python
  def _replay_backlog(self, ack: HandshakeAck | None, sock: socket.socket) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    backlog = self._history.find_after(ack.last_record_id, ack.last_received_at)
    if backlog is None:
      logger.warning(
        "Log server last confirmed record id %s for %r, but it could not be located in history; "
        "some records may already have aged out. Resuming live.",
        ack.last_record_id,
        self._program_name,
      )
      return True
    for entry in backlog:
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        return False
      self._last_sent_id = entry.id
    return True
```

with:

```python
  def _replay_backlog(self, ack: HandshakeAck | None, sock: socket.socket) -> bool:
    """Resend whatever the server's ack says it is missing. Returns ``False`` if the connection died."""
    if ack is None:
      return True
    for entry in self._durability.resolve_backlog(ack):
      payload = orjson.dumps(record_to_payload(entry.record), default=str)
      try:
        sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)
      except OSError:
        return False
      self._durability.mark_sent(entry.id)
    return True
```

- [ ] **Step 8: Rewrite `_sleep_or_drain`'s emergency-loop bookkeeping**

Replace the guard `if self._emergency_writer is None:` with `if self._emergency.writer is None:`.

Replace the loop body:

```python
      record_id = self._next_id
      self._next_id += 1
      record.record_id = record_id
      entry = HistoryEntry(id=record_id, created=record.created, record=record)  # type: ignore[arg-type]
      self._history.append(entry)
      self._id_checkpoint.schedule_persist(record_id)
      entry.persisted = True
      self._emergency_writer.submit(entry)
      if self._local_root is not None:
        self._local_root.handle(record)
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
```

with:

```python
      self._durability.record(record, local_root=self._local_root, emergency_writer=self._emergency.writer)  # type: ignore[arg-type]
      if hasattr(self._queue, "task_done"):
        self._queue.task_done()  # type: ignore[attr-defined]
```

- [ ] **Step 9: Delete `_maybe_enter_emergency_mode` and `_exit_emergency_mode`**

Delete both methods entirely from `ThreadedQueueDrainer`.

- [ ] **Step 10: Confirm no test file reaches into this class's moved private attributes**

Run: `grep -n "_emergency_writer\|_consecutive_failures\|_maybe_enter_emergency_mode\|_exit_emergency_mode\|_last_success_monotonic\|_read_message\|_replay_backlog\|_last_sent_id\|_history\b\|_id_checkpoint\b" tests/central_log_server/test_client_queue_drainers.py`
Expected: only the `AsyncioQueueDrainer` test already migrated in Task 5 matches (now using `_emergency.*`,
not the old names) - no `ThreadedQueueDrainer`-specific matches. If any are found, migrate them the same
way as Task 5 Step 11 before continuing.

- [ ] **Step 11: Run the queue-drainer and server-integration test files**

Run: `uv run pytest tests/central_log_server/test_client_queue_drainers.py tests/central_log_server/test_client_server_integration.py -v`
Expected: PASS (all tests)

- [ ] **Step 12: Lint and type-check**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py`
Expected: `All checks passed!`

Run: `uv run pyright src/aeth_ext/central_log_server/client/__init__.py`
Expected: no new errors versus the Task 3 baseline.

- [ ] **Step 13: Commit**

```bash
git add src/aeth_ext/central_log_server/client/__init__.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): wire ThreadedQueueDrainer to RecordDurability/EmergencyModeTracker

Fifth and final per-class migration step of deduplicating the three
client transport classes (PR review comment on client/__init__.py).
Same shape as the AsyncioQueueDrainer migration, adapted for this
class's synchronous/threaded reconnect loop. All three transport
classes now share RecordDurability, EmergencyModeTracker, and the
read_server_message_sync/async free functions instead of duplicating
this logic three times over.
EOF
)"
```

---

## Task 7: Cleanup pass and full regression run

**Files:**
- Modify: `src/aeth_ext/central_log_server/client/__init__.py` (remove now-unused imports)

**Interfaces:** None new - this task only removes dead code and verifies the whole refactor.

- [ ] **Step 1: Find unused imports**

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py`
Expected: one or more `F401 [*] '...' imported but unused` findings. Based on the migration in Tasks 4-6,
expect `RecordHistoryBuffer`, `EmergencyHistoryWriter`, `AsyncioIdCheckpointBackend`, `IdCheckpointBackend`,
and `ThreadedIdCheckpointBackend` to no longer be referenced directly in this file (all now used only
inside `durability.py`/`emergency.py`). `HistoryEntry` is expected to remain used (as the return type of
`self._durability.record(...)` calls' implicit local variable and wherever `entry.record`/`entry.id` are
accessed).

- [ ] **Step 2: Remove the unused imports**

Remove exactly what Step 1 flagged from the import block at the top of
`src/aeth_ext/central_log_server/client/__init__.py`. Do not remove `HistoryEntry` unless ruff also flags
it - confirm by re-running the Step 1 command after each removal.

- [ ] **Step 3: Confirm the module still imports cleanly and ruff is fully clean**

Run: `uv run python -c "import aeth_ext.central_log_server.client"`
Expected: no output, exit code 0

Run: `uv run ruff check src/aeth_ext/central_log_server/client/__init__.py`
Expected: `All checks passed!`

- [ ] **Step 4: Full pyright pass on the whole client package**

Run: `uv run pyright src/aeth_ext/central_log_server/client/`
Expected: no new errors versus the pre-existing baseline noted in Task 3 Step 5 (the
`TaggedLogRecord`/`LogRecord` handler-override mismatches that predate this refactor).

- [ ] **Step 5: Full regression run of the central_log_server test subtree**

Run: `uv run pytest tests/central_log_server/ -v`
Expected: PASS (all tests - this is the first and only whole-subtree run in this plan, per this repo's
testing-workflow convention of not running broad suites eagerly on a feature branch)

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/central_log_server/client/__init__.py
git commit -m "$(cat <<'EOF'
refactor(central_log_server): remove imports left unused by the composition refactor

Final cleanup step after wiring all three client transport classes to
RecordDurability/EmergencyModeTracker/read_server_message_sync/async
(PR review comment on client/__init__.py) - removes the
RecordHistoryBuffer/EmergencyHistoryWriter/id_checkpoint-backend
imports that were only needed for the now-deleted duplicated
construction logic.
EOF
)"
```
