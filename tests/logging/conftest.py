"""Shared pytest fixtures for the `dict_config` test suite.

The cross-cutting autouse isolation fixtures (`_isolate_runtime_registry`,
`_isolate_logging_state`) live in the root `tests/conftest.py` and apply here
automatically.
"""

# Third party imports
import pytest

# First party imports
from aeth_ext.logging.bases import TaggedLogRecord


@pytest.fixture(autouse=True)
def _project_name_set(monkeypatch: pytest.MonkeyPatch) -> None:
  """Stand in for a consuming program's ``PROJECT_NAME`` entrypoint constant.

  `TaggedLogRecord._PROJECT_NAME` is resolved once, at `bases.py` import time,
  by walking up from the caller to the process entrypoint looking for a
  `PROJECT_NAME` constant (see `parse_and_grab_constants`). Under pytest there
  is no such constant anywhere in the ancestry, so it resolves to the `"FIX_ME"`
  sentinel and any real record creation raises. Tests that actually emit log
  records need a stand-in value, same as a real consumer would provide one.
  """
  monkeypatch.setattr(TaggedLogRecord, "_PROJECT_NAME", "aeth_ext-tests")
