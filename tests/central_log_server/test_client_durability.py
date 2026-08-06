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

_FIRST_ID = 1
_SECOND_ID = 2
_MARKED_SENT_ID = 5


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

    assert first.id == _FIRST_ID
    assert second.id == _SECOND_ID
    assert first.record.record_id == _FIRST_ID
    assert second.record.record_id == _SECOND_ID

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

    durability.mark_sent(_MARKED_SENT_ID)

    assert durability.last_sent_id == _MARKED_SENT_ID


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

    assert not durability._id_checkpoint._thread.is_alive()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
