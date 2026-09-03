# Standard library imports
import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Third party imports
import orjson
from rich.style import NULL_STYLE

# First party imports
from aeth_ext.central_log_server.protocol import LENGTH_STRUCT
from aeth_ext.central_log_server.web_viewer.screens import log_picker as log_picker_module
from aeth_ext.central_log_server.web_viewer.screens.log_picker import (
  ConfirmDeleteModal,
  FileChosen,
  LogFileTree,
  LogPickerScreen,
)

# Local folder imports
# Local imports
from .conftest import ScreenHostApp, expand_and_get_child, write_log_file

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  import pytest

_PUSHED_RECORD_ID = 5


class TestFileSelection:
  async def test_selecting_a_log_file_posts_file_chosen_with_its_path(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    log_path = write_log_file(program_dir, "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    chosen: list[FileChosen] = []

    # A message that bubbles (FileChosen's default) triggers message_hook once
    # per message pump it passes through on the way up, not once overall -- so
    # the same FileChosen instance fires the hook again at the App after the
    # Screen handles it. Dedupe by identity to count actual selections only.
    def _capture_file_chosen(m: object) -> None:
      if isinstance(m, FileChosen) and m not in chosen:
        chosen.append(m)

    async with app.run_test(message_hook=_capture_file_chosen) as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")

      tree.select_node(file_node)
      await pilot.pause()

      assert [c.path for c in chosen] == [log_path]

  async def test_selecting_a_non_log_file_warns_and_does_not_post_file_chosen(self, tmp_path: Path) -> None:
    write_log_file(tmp_path, "app.log", ["hello"])  # unused, keeps the dir non-empty
    program_dir = tmp_path / "acme"
    program_dir.mkdir()
    other_path = program_dir / "notes.json"
    other_path.write_text("{}", encoding="utf-8")
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    chosen: list[FileChosen] = []
    notify_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    # A message that bubbles (FileChosen's default) triggers message_hook once
    # per message pump it passes through on the way up, not once overall -- so
    # the same FileChosen instance fires the hook again at the App after the
    # Screen handles it. Dedupe by identity to count actual selections only.
    def _capture_file_chosen(m: object) -> None:
      if isinstance(m, FileChosen) and m not in chosen:
        chosen.append(m)

    async with app.run_test(message_hook=_capture_file_chosen) as pilot:
      await pilot.pause()
      app.notify = lambda *a, **_kw: notify_calls.append((a, _kw))

      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "notes.json")

      tree.select_node(file_node)
      await pilot.pause()

      assert chosen == []
      assert notify_calls, "expected a warning notification for a non .log/.txt file"


class TestRefreshAction:
  async def test_pressing_r_reloads_the_tree_and_picks_up_new_files(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    write_log_file(program_dir, "first.log", ["one"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      await expand_and_get_child(pilot, dir_node, "first.log")

      write_log_file(program_dir, "second.log", ["two"])
      await pilot.press("r")
      await pilot.pause()

      # `reload()` rebuilds nodes from scratch (see DirectoryTree._reload), so
      # the pre-refresh `dir_node` reference is now a stale, orphaned node --
      # its own `.children` won't reflect the reload. Re-query the live one.
      refreshed_dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      names = {c.data.path.name for c in refreshed_dir_node.children if c.data is not None}
      assert "second.log" in names


class TestDeleteFile:
  async def test_deleting_with_nothing_highlighted_warns_and_deletes_nothing(self, tmp_path: Path) -> None:
    log_path = write_log_file(tmp_path / "acme", "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    notify_calls: list[Any] = []

    async with app.run_test() as pilot:
      await pilot.pause()
      app.notify = lambda *a, **_kw: notify_calls.append(a)

      await pilot.press("d")
      await pilot.pause()

      assert log_path.exists()
      assert notify_calls

  async def test_cancelling_the_confirmation_modal_keeps_the_file(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    log_path = write_log_file(program_dir, "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")
      tree.move_cursor(file_node)

      await pilot.press("d")
      await pilot.pause()
      assert isinstance(app.screen, ConfirmDeleteModal)

      await pilot.click("#delete-cancel")
      await pilot.pause()

      assert log_path.exists()

  async def test_confirming_the_modal_deletes_the_file_and_notifies(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    log_path = write_log_file(program_dir, "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    notify_calls: list[Any] = []

    async with app.run_test() as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")
      tree.move_cursor(file_node)

      await pilot.press("d")
      await pilot.pause()
      modal = app.screen
      assert isinstance(modal, ConfirmDeleteModal)
      modal.notify = lambda *a, **_kw: notify_calls.append(a)

      await pilot.click("#delete-confirm")
      await pilot.pause()

      assert not log_path.exists()


class TestLogFileTreeRenderLabel:
  async def test_connected_program_gets_the_connected_badge_and_todays_id_count(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    log_path = write_log_file(program_dir, "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")
      assert file_node.data is not None
      assert file_node.data.path == log_path

      tree._apply_packet(  # pyright: ignore[reportPrivateUsage]
        {
          "connected_programs": ["acme"],
          "current_ids": {"acme": 42},
          "midnight_ids": {"acme": 10},
          "midnight_date": datetime.now(tz=log_picker_module.settings.tz).date().isoformat(),
        }
      )

      label = tree.render_label(file_node, NULL_STYLE, NULL_STYLE)
      plain = label.plain
      assert "acme" in plain
      assert "32 IDs" in plain  # 42 - 10

      badge_style = next(seg.style for seg in label.render(app.console) if "acme" in seg.text)
      assert "dark_green" in str(badge_style)

  async def test_disconnected_program_gets_the_disconnected_badge(self, tmp_path: Path) -> None:
    program_dir = tmp_path / "acme"
    write_log_file(program_dir, "app.log", ["hello"])
    screen = LogPickerScreen(tmp_path)
    app = ScreenHostApp(screen)

    async with app.run_test() as pilot:
      await pilot.pause()
      tree = screen.query_one(LogFileTree)
      dir_node = next(c for c in tree.root.children if c.data and c.data.path == program_dir)
      file_node = await expand_and_get_child(pilot, dir_node, "app.log")

      tree._apply_packet(  # pyright: ignore[reportPrivateUsage]
        {"connected_programs": [], "current_ids": {}, "midnight_ids": {}, "midnight_date": ""}
      )

      label = tree.render_label(file_node, NULL_STYLE, NULL_STYLE)
      badge_style = next(seg.style for seg in label.render(app.console) if "acme" in seg.text)
      assert "dark_red" in str(badge_style)


class TestLiveMetadataPush:
  async def test_a_pushed_connect_event_updates_the_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program_dir = tmp_path / "acme"
    write_log_file(program_dir, "app.log", ["hello"])

    received_handshake = asyncio.Event()

    async def handle_subscriber(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
      received_handshake.set()
      packet = {
        "type": "event",
        "event": "connected",
        "program": "acme",
        "snapshot": {
          "connected_programs": ["acme"],
          "current_ids": {"acme": _PUSHED_RECORD_ID},
          "midnight_ids": {},
          "midnight_date": "",
        },
      }
      data = orjson.dumps(packet)
      writer.write(LENGTH_STRUCT.pack(len(data)) + data)
      await writer.drain()
      await asyncio.sleep(0.5)
      writer.close()

    server = await asyncio.start_server(handle_subscriber, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(log_picker_module.settings, "state_push_host", "127.0.0.1")
    monkeypatch.setattr(log_picker_module.settings, "state_push_port", port)

    async with server:
      screen = LogPickerScreen(tmp_path)
      app = ScreenHostApp(screen)

      async with app.run_test() as pilot:
        await asyncio.wait_for(received_handshake.wait(), timeout=5)
        tree = screen.query_one(LogFileTree)
        for _ in range(20):
          await pilot.pause(0.1)
          if "acme" in tree._connected_programs:  # pyright: ignore[reportPrivateUsage]
            break

        assert "acme" in tree._connected_programs  # pyright: ignore[reportPrivateUsage]
        assert tree._current_ids.get("acme") == _PUSHED_RECORD_ID  # pyright: ignore[reportPrivateUsage]
