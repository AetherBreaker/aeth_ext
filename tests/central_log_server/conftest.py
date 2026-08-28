"""Shared pytest fixtures for the central log server test suite.

The cross-cutting autouse isolation fixtures (`_isolate_runtime_registry`,
`_isolate_logging_state`, `_clear_fatal_event`) live in the root
`tests/conftest.py` and apply here automatically.
"""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_emergency_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  """Keep the persisted emergency diagnostics file inside tmp_path for every test."""
  # First party imports
  from aeth_ext.logging import emergency_diagnostics

  monkeypatch.setattr(emergency_diagnostics, "emergency_diagnostics_path", tmp_path / "emergency_diagnostics.txt")
