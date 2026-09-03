# Standard library imports
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.central_log_server.client.id_checkpoint import (
  AsyncioIdCheckpointBackend,
  ThreadedIdCheckpointBackend,
)

if TYPE_CHECKING:
  # Third party imports
  import pytest

_PERSISTED_ID = 7
_LATEST_OF_A_BURST = 3
_EXPECTED_REPLACE_ATTEMPTS = 2


class TestFileCheckpointLoad:
  def test_missing_file_loads_as_zero(self, tmp_path: Path) -> None:
    backend = ThreadedIdCheckpointBackend(tmp_path / "missing.txt")
    try:
      assert backend.load() == 0
    finally:
      backend.close()

  def test_garbage_content_loads_as_zero(self, tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.txt"
    path.write_text("not-an-int", encoding="utf-8")
    backend = ThreadedIdCheckpointBackend(path)
    try:
      assert backend.load() == 0
    finally:
      backend.close()

  def test_previously_persisted_id_round_trips(self, tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.txt"
    first = ThreadedIdCheckpointBackend(path)
    first.schedule_persist(_PERSISTED_ID)
    first.close()

    second = ThreadedIdCheckpointBackend(path)
    try:
      assert second.load() == _PERSISTED_ID
    finally:
      second.close()


class TestThreadedIdCheckpointBackend:
  def test_a_burst_of_schedule_persist_calls_coalesces_to_only_the_latest(self, tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.txt"
    backend = ThreadedIdCheckpointBackend(path)

    backend.schedule_persist(1)
    backend.schedule_persist(2)
    backend.schedule_persist(_LATEST_OF_A_BURST)
    backend.close()

    assert path.read_text(encoding="utf-8").strip() == str(_LATEST_OF_A_BURST)

  def test_close_joins_the_background_thread(self, tmp_path: Path) -> None:
    backend = ThreadedIdCheckpointBackend(tmp_path / "checkpoint.txt")

    backend.close()

    assert not backend._thread.is_alive()  # pyright: ignore[reportPrivateUsage]

  def test_write_falls_back_to_unlink_then_replace_on_permission_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "checkpoint.txt"
    path.write_text("old", encoding="utf-8")
    real_replace = Path.replace
    call_count = 0

    def flaky_replace(self: Path, target: Path) -> Path:
      nonlocal call_count
      call_count += 1
      if call_count == 1:
        raise PermissionError("simulated lock")
      return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    backend = ThreadedIdCheckpointBackend(path)

    backend.schedule_persist(_PERSISTED_ID)
    backend.close()

    assert call_count == _EXPECTED_REPLACE_ATTEMPTS
    assert path.read_text(encoding="utf-8").strip() == str(_PERSISTED_ID)


class TestAsyncioIdCheckpointBackend:
  async def test_schedule_persist_writes_via_the_running_loop(self, tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.txt"
    loop = asyncio.get_running_loop()
    backend = AsyncioIdCheckpointBackend(path, loop)

    backend.schedule_persist(_PERSISTED_ID)

    for _ in range(50):
      if path.exists():
        break
      await asyncio.sleep(0.02)

    assert path.read_text(encoding="utf-8").strip() == str(_PERSISTED_ID)

  async def test_close_is_a_harmless_noop(self, tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    backend = AsyncioIdCheckpointBackend(tmp_path / "checkpoint.txt", loop)

    backend.close()
