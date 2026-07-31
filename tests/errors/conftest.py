"""Shared pytest fixtures for the `errors` test suite."""

# Third party imports
import pytest

# First party imports
from aeth_ext.errors.err_handling import FATAL_EVENT


@pytest.fixture(autouse=True)
def _clear_fatal_event():
  """Fail loudly if a test tripped the (one-shot) process-wide fatal flag.

  Every scenario that actually sets FATAL_EVENT is exercised in an isolated
  subprocess (see test_err_handling.py's `-O` helper), so this in-process
  guard should never legitimately fire.
  """
  yield

  assert not FATAL_EVENT.is_set(), "test left FATAL_EVENT set; aiologic events cannot be reset"
