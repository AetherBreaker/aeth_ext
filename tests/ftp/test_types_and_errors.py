"""Tests for `aeth_ext.ftp.errors` and `aeth_ext.ftp.types`."""

# Standard library imports
from datetime import UTC, datetime

# First party imports
from aeth_ext.ftp.errors import ServerNotAvailableError
from aeth_ext.ftp.types import ListDirResult


class TestServerNotAvailableError:
  def test_is_a_connection_error(self) -> None:
    assert issubclass(ServerNotAvailableError, ConnectionError)

  def test_can_be_raised_and_caught_as_connection_error(self) -> None:
    try:
      raise ServerNotAvailableError("server offline")
    except ConnectionError as e:
      assert str(e) == "server offline"


class TestListDirResult:
  def test_is_a_named_tuple_with_filename_and_modified_time(self) -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)

    result = ListDirResult(filename="file.txt", modified_time=when)

    assert result.filename == "file.txt"
    assert result.modified_time == when
    assert result == ("file.txt", when)
