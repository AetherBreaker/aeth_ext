# Standard library imports
import threading
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.monitoring import health
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator
  from pathlib import Path

  # Third party imports
  from pydantic import SecretStr

  type FailPing = tuple[SecretStr | None, bool, bool, str | None]
  """A recorded ``/fail`` from ``health``: ``(url, failure, autoprovision, body)``."""


@pytest.fixture
def health_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
  monkeypatch.setattr(BaseSettings.get_settings(), "log_loc_folder", tmp_path / "logs")
  return tmp_path / "logs" / "health"


@pytest.fixture(autouse=True)
def fail_pings(monkeypatch: pytest.MonkeyPatch) -> Generator[list[FailPing]]:
  """Records ``health``'s failure-ping ``/fail`` requests instead of sending them, and joins their threads."""
  calls: list[FailPing] = []
  threads: list[threading.Thread] = []

  def record(
    url: SecretStr | None, *, failure: bool = False, start: bool = False, autoprovision: bool = False, body: str | None = None
  ) -> None:
    del start
    threads.append(threading.current_thread())
    calls.append((url, failure, autoprovision, body))

  monkeypatch.setattr(health, "ping_healthcheck", record)
  yield calls
  assert threading.current_thread() not in threads, "a failure ping's /fail ran on the caller's thread"
  for thread in threads:
    thread.join(timeout=5)
