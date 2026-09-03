# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_emergency_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  # First party imports
  from aeth_ext.logging import emergency_diagnostics

  monkeypatch.setattr(emergency_diagnostics, "emergency_diagnostics_path", tmp_path / "emergency_diagnostics.txt")
