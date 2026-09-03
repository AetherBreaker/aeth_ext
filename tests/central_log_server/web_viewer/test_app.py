# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest
from textual.widgets import RichLog

# First party imports
from aeth_ext.central_log_server import web_viewer as app_module
from aeth_ext.central_log_server.web_viewer import LogWebViewApp
from aeth_ext.central_log_server.web_viewer.screens.log_picker import LogFileTree, LogPickerScreen
from aeth_ext.central_log_server.web_viewer.screens.log_stream import LogStreamScreen

# Local folder imports
# Local imports
from .conftest import expand_and_get_child, write_log_file

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


def _resolved(path: Path) -> Path:
  return path.resolve()


class TestStartup:
  async def test_mounts_the_picker_screen_rooted_at_the_given_log_root(self, tmp_path: Path) -> None:
    resolved_log_root = _resolved(tmp_path)
    app = LogWebViewApp(log_root=tmp_path)

    async with app.run_test() as pilot:
      await pilot.pause()
      assert isinstance(app.screen, LogPickerScreen)
      assert app.screen._log_root == resolved_log_root  # pyright: ignore[reportPrivateUsage]


class TestPickerToStreamNavigation:
  async def test_selecting_a_file_pushes_the_stream_screen_and_streams_its_content(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    log_path = write_log_file(program_dir, "app.log", ["hello from the stream screen"])
    app = LogWebViewApp(log_root=tmp_path)

    async with app.run_test() as pilot:
      await pilot.pause()
      picker = app.screen
      assert isinstance(picker, LogPickerScreen)

      tree = picker.query_one("#log-tree", LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")
      tree.select_node(file_node)
      await pilot.pause()

      stream_screen = app.screen
      assert isinstance(stream_screen, LogStreamScreen)
      assert stream_screen._log_path == log_path  # pyright: ignore[reportPrivateUsage]

      log_widget = stream_screen.query_one("#stream-log", RichLog)
      assert [strip.text for strip in log_widget.lines] == ["hello from the stream screen"]

  async def test_escape_from_the_stream_screen_returns_to_the_picker(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    write_log_file(program_dir, "app.log", ["hello"])
    app = LogWebViewApp(log_root=tmp_path)

    async with app.run_test() as pilot:
      await pilot.pause()
      picker = app.screen
      assert isinstance(picker, LogPickerScreen)
      tree = picker.query_one("#log-tree", LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")
      tree.select_node(file_node)
      await pilot.pause()
      assert isinstance(app.screen, LogStreamScreen)

      await pilot.press("escape")
      await pilot.pause()

      assert app.screen is picker


class TestHandleException:
  async def test_uncaught_exception_alerts_before_the_app_still_crashes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alert_calls: list[tuple[str, BaseException]] = []
    monkeypatch.setattr(app_module, "alert_exception", lambda label, exc: alert_calls.append((label, exc)))

    def _boom(self: LogPickerScreen) -> None:
      raise RuntimeError("simulated crash")

    monkeypatch.setattr(LogPickerScreen, "compose", _boom)

    app = LogWebViewApp(log_root=tmp_path)
    with pytest.raises(RuntimeError, match="simulated crash"):
      async with app.run_test():
        pass

    assert len(alert_calls) == 1
    label, exc = alert_calls[0]
    assert label == "LogWebViewApp"
    assert isinstance(exc, RuntimeError)
