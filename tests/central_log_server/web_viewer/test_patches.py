"""Tests for `aeth_ext.central_log_server.web_viewer.patches.WebViewerPatches`."""

# Standard library imports
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.web_viewer import patches as wv_patches

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator


@pytest.fixture(autouse=True)
def _restore_main_module() -> Generator[None]:
  """`_is_web_viewer_entrypoint` reads `sys.modules["__main__"]`; restore it after."""
  original = sys.modules.get("__main__")

  yield

  if original is not None:
    sys.modules["__main__"] = original


class TestIsWebViewerEntrypoint:
  def test_true_when_main_file_is_web_viewers_own_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
    real_main_file = Path(wv_patches.__file__).parent / "__main__.py"
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(real_main_file)))

    assert wv_patches._is_web_viewer_entrypoint() is True  # pyright: ignore[reportPrivateUsage]

  def test_false_for_a_different_entrypoint_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other_file = tmp_path / "some_other_script.py"
    other_file.write_text("")
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(other_file)))

    assert wv_patches._is_web_viewer_entrypoint() is False  # pyright: ignore[reportPrivateUsage]

  def test_false_when_main_has_no_file_attribute(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace())

    assert wv_patches._is_web_viewer_entrypoint() is False  # pyright: ignore[reportPrivateUsage]


class TestPatchTextualLoggerGuard:
  def test_does_nothing_when_not_the_web_viewer_entrypoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__="/somewhere/else/main.py"))

    # Third party imports
    from textual import Logger as TextualLogger

    original_call = TextualLogger.__call__

    wv_patches.WebViewerPatches.patch_textual_logger()  # pyright: ignore[reportAttributeAccessIssue]

    assert TextualLogger.__call__ is original_call

  def test_patches_textual_logger_call_when_it_is_the_entrypoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
    real_main_file = Path(wv_patches.__file__).parent / "__main__.py"
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(real_main_file)))

    # Third party imports
    from textual import Logger as TextualLogger

    original_call = TextualLogger.__call__
    try:
      wv_patches.WebViewerPatches.patch_textual_logger()  # pyright: ignore[reportAttributeAccessIssue]

      assert TextualLogger.__call__ is not original_call
    finally:
      TextualLogger.__call__ = original_call

  def test_routes_textual_log_calls_through_stdlib_logging(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    real_main_file = Path(wv_patches.__file__).parent / "__main__.py"
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__file__=str(real_main_file)))

    # Third party imports
    from textual import Logger as TextualLogger
    from textual.app import App

    original_call = TextualLogger.__call__
    try:
      wv_patches.WebViewerPatches.patch_textual_logger()  # pyright: ignore[reportAttributeAccessIssue]

      app = App()
      with caplog.at_level("DEBUG", logger="textual"):
        app.log("hello from textual")

      assert any("hello from textual" in record.message for record in caplog.records)
    finally:
      TextualLogger.__call__ = original_call
