"""Tests for `aeth_ext.central_log_server.patches.CentralLogServerPatches`."""

# Standard library imports
import io
import logging
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.patches import CentralLogServerPatches
from aeth_ext.logging.bases import SmartColumnFormatter

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator


@pytest.fixture
def _emit_separator_patch_applied() -> Iterator[None]:
  """Apply the patch for one test, then remove it so it never leaks to others."""
  CentralLogServerPatches.patch_handler_emit_separator()  # pyright: ignore[reportAttributeAccessIssue]
  try:
    yield
  finally:
    if hasattr(logging.Handler, "emit_separator"):
      del logging.Handler.emit_separator  # pyright: ignore[reportAttributeAccessIssue]


@pytest.fixture
def stream_handler(_emit_separator_patch_applied: None) -> logging.StreamHandler[io.StringIO]:
  stream = io.StringIO()
  handler = logging.StreamHandler(stream)
  return handler


class TestPatchIsApplied:
  def test_installs_emit_separator_on_handler_class(self, _emit_separator_patch_applied: None):
    assert hasattr(logging.Handler, "emit_separator")


class TestEmitSeparatorWithPlainFormatter:
  def test_writes_message_centered_in_dashes(self, stream_handler: logging.StreamHandler[io.StringIO]):
    stream_handler.emit_separator(" hello connected ")  # pyright: ignore[reportAttributeAccessIssue]

    output = stream_handler.stream.getvalue()
    assert " hello connected " in output
    assert output.strip("\n").count("-") > 0

  def test_no_formatter_still_writes_centered_message(self, stream_handler: logging.StreamHandler[io.StringIO]):
    stream_handler.setFormatter(None)

    stream_handler.emit_separator(" no formatter ")  # pyright: ignore[reportAttributeAccessIssue]

    assert " no formatter " in stream_handler.stream.getvalue()


class TestEmitSeparatorWithSmartColumnFormatter:
  def test_delegates_to_format_separator(
    self, stream_handler: logging.StreamHandler[io.StringIO], monkeypatch: pytest.MonkeyPatch
  ):
    formatter = SmartColumnFormatter(columns=["{message}"], persist_path=None)
    stream_handler.setFormatter(formatter)
    calls: list[tuple[str, str | None]] = []
    real_format_separator = formatter.format_separator

    def spy(message: str, handler_name: str | None = None) -> str:
      calls.append((message, handler_name))
      return real_format_separator(message, handler_name)

    monkeypatch.setattr(formatter, "format_separator", spy)

    stream_handler.emit_separator(" connected ")  # pyright: ignore[reportAttributeAccessIssue]

    assert calls
    assert calls[0][0] == " connected "
    assert " connected " in stream_handler.stream.getvalue()


class TestEmitSeparatorEdgeCases:
  def test_handler_without_a_stream_attribute_is_a_no_op(self, _emit_separator_patch_applied: None):
    class _NoStreamHandler(logging.Handler):
      pass

    handler = _NoStreamHandler()

    handler.emit_separator("no stream here")  # pyright: ignore[reportAttributeAccessIssue]  # must not raise

  def test_write_failure_is_swallowed(self, stream_handler: logging.StreamHandler[io.StringIO]):
    def raise_on_write(_text: str) -> int:
      raise OSError("stream closed")

    stream_handler.stream.write = raise_on_write

    stream_handler.emit_separator("closed stream")  # pyright: ignore[reportAttributeAccessIssue]  # must not raise
