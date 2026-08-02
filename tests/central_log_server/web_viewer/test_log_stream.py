"""Pilot-driven tests for `aeth_ext.central_log_server.web_viewer.screens.log_stream`."""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
from textual.widgets import RichLog
from textual.widgets._input import Selection

# First party imports
from aeth_ext.central_log_server.web_viewer.screens.log_stream import (
  FindBar,
  LogStreamScreen,
  _FindInput,  # pyright: ignore[reportPrivateUsage]
)

# Local folder imports
# Local imports
from .conftest import ScreenHostApp, write_log_file

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable
  from pathlib import Path

  # Third party imports
  from textual.pilot import Pilot

_FILE_WATCH_POLL = 0.2
_FILE_WATCH_ATTEMPTS = 25  # ~5s ceiling for the real OS file watcher to notice a change


async def _wait_until(pilot: Pilot[None], predicate: Callable[[], bool]) -> bool:
  for _ in range(_FILE_WATCH_ATTEMPTS):
    if predicate():
      return True
    await pilot.pause(_FILE_WATCH_POLL)
  return False


class TestInitialTail:
  async def test_shows_the_tail_of_an_existing_file(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["line one", "line two", "line three"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      log_widget = screen.query_one("#stream-log", RichLog)
      assert [strip.text for strip in log_widget.lines] == ["line one", "line two", "line three"]

  async def test_reports_a_missing_file_instead_of_crashing(self, tmp_path: Path):
    screen = LogStreamScreen(tmp_path / "does_not_exist.log")
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      log_widget = screen.query_one("#stream-log", RichLog)
      [line] = [strip.text for strip in log_widget.lines]
      assert "does_not_exist.log" in line


class TestRefresh:
  async def test_reloads_the_file_from_scratch(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["first version"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      log_path.write_text("second version\n", encoding="utf-8")
      await pilot.press("r")
      await pilot.pause()

      log_widget = screen.query_one("#stream-log", RichLog)
      assert [strip.text for strip in log_widget.lines] == ["second version"]


class TestLiveTail:
  async def test_appended_lines_are_picked_up_by_the_file_watcher(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["initial line"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      with log_path.open("a", encoding="utf-8") as f:
        f.write("appended line\n")

      log_widget = screen.query_one("#stream-log", RichLog)
      seen = await _wait_until(pilot, lambda: any(strip.text == "appended line" for strip in log_widget.lines))

      assert seen, "watcher never picked up the appended line"
      assert [strip.text for strip in log_widget.lines] == ["initial line", "appended line"]


class TestFindBar:
  async def test_ctrl_f_opens_the_find_bar_and_focuses_it(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["hello world"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      bar = screen.query_one("#find-bar", FindBar)
      assert not bar.is_open

      await pilot.press("ctrl+f")

      assert bar.is_open
      assert isinstance(app.focused, _FindInput)

  async def test_typing_a_term_highlights_matching_text(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["the quick brown fox"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.press("b", "r", "o", "w", "n")
      await pilot.pause(0.3)  # let the 0.12s debounce timer fire

      log_widget = screen.query_one("#stream-log", RichLog)
      [strip] = log_widget.lines
      highlighted = [seg.text for seg in strip if seg.style is not None and "yellow" in str(seg.style)]
      assert highlighted == ["brown"]

  async def test_unchecking_highlight_all_removes_the_highlight(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["the quick brown fox"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.press("f", "o", "x")
      await pilot.pause(0.3)

      await pilot.click("#cb-highlight")
      await pilot.pause(0.1)

      log_widget = screen.query_one("#stream-log", RichLog)
      [strip] = log_widget.lines
      highlighted = [seg.text for seg in strip if seg.style is not None and "yellow" in str(seg.style)]
      assert highlighted == []

  async def test_match_case_makes_the_search_case_sensitive(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["Fox and fox"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.press("f", "o", "x")
      await pilot.pause(0.3)

      log_widget = screen.query_one("#stream-log", RichLog)

      def highlighted_terms() -> list[str]:
        [strip] = log_widget.lines
        return [seg.text for seg in strip if seg.style is not None and "yellow" in str(seg.style)]

      assert highlighted_terms() == ["Fox", "fox"]

      await pilot.click("#cb-case")
      await pilot.pause(0.1)

      assert highlighted_terms() == ["fox"]

  async def test_invalid_regex_does_not_crash_and_shows_no_highlight(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["the quick brown fox"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.click("#cb-regex")
      await pilot.press("(", "u", "n", "c", "l", "o", "s", "e", "d")
      await pilot.pause(0.3)

      log_widget = screen.query_one("#stream-log", RichLog)
      [strip] = log_widget.lines
      assert strip.text == "the quick brown fox"
      assert all(seg.style is None or "yellow" not in str(seg.style) for seg in strip)

  async def test_close_button_closes_the_bar_and_clears_highlighting(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["the quick brown fox"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.press("f", "o", "x")
      await pilot.pause(0.3)

      bar = screen.query_one("#find-bar", FindBar)
      await pilot.click("#find-close")
      await pilot.pause()

      assert not bar.is_open
      log_widget = screen.query_one("#stream-log", RichLog)
      [strip] = log_widget.lines
      assert all(seg.style is None or "yellow" not in str(seg.style) for seg in strip)

  async def test_escape_closes_the_find_bar_before_dismissing_the_screen(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["hello"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      bar = screen.query_one("#find-bar", FindBar)
      assert bar.is_open

      await pilot.press("escape")
      assert not bar.is_open
      assert app.screen is screen  # first escape only closes the bar

      await pilot.press("escape")
      await pilot.pause()
      assert app.screen is not screen  # second escape dismisses the screen


class TestCtrlAInFindInput:
  """Regression test for the Ctrl+A fix: it must select-all, not jump to line start.

  Textual's `Input` binds ctrl+a to "home" (readline-style) by default, which
  is surprising in a find/search bar; `_FindInput.BINDINGS` overrides it to
  select-all instead (see log_stream.py).
  """

  async def test_ctrl_a_selects_the_entire_search_term_instead_of_moving_the_cursor_home(self, tmp_path: Path):
    log_path = write_log_file(tmp_path, "app.log", ["hello"])
    screen = LogStreamScreen(log_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      await pilot.press("ctrl+f")
      await pilot.press("n", "e", "e", "d", "l", "e")

      finder = screen.query_one("#find-input", _FindInput)
      assert finder.cursor_position == len("needle")

      await pilot.press("ctrl+a")

      assert finder.selection == Selection(0, len("needle"))
      assert finder.cursor_position != 0  # would be 0 if ctrl+a had instead run "home"
