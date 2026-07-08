# Standard library imports
import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

# Third party imports
import orjson
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Label, ListItem, ListView, Static

# First party imports
from aeth_ext.central_log_server.settings import Settings
from aeth_ext.central_log_server.web_viewer.screens.program_screen import ProgramScreen
from aeth_ext.central_log_server.web_viewer.wake import read_wake_token

if TYPE_CHECKING:
  # Third party imports
  from textual.app import ComposeResult

  # First party imports
  from aeth_ext.command_client.client import CommandClient


settings = Settings.get_settings()


def _newest_mtime(root: Path) -> float | None:
  """Return the newest file mtime anywhere under ``root``, or ``None`` if empty."""
  newest: float | None = None
  stack: list[str] = [str(root)]
  while stack:
    current = stack.pop()
    try:
      with os.scandir(current) as it:
        for entry in it:
          try:
            if entry.is_dir(follow_symlinks=False):
              stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
              mtime = entry.stat().st_mtime
              if newest is None or mtime > newest:
                newest = mtime
          except OSError:
            continue
    except OSError:
      continue
  return newest


class ProgramRow(ListItem):
  """A single selectable program entry, colour-coded by connection state."""

  _STATUS_LABELS: ClassVar[dict[str, str]] = {
    "both": "logs + commands",
    "command-only": "commands only",
    "log-only": "logs only",
  }

  def __init__(self, program_name: str, category: str, mtime_text: str) -> None:
    super().__init__(classes=category)
    self.program_name = program_name
    self._category = category
    self._mtime_text = mtime_text

  @override
  def compose(self) -> ComposeResult:
    with Horizontal(classes="program-row"):
      yield Static("\u25cf", classes="program-badge")
      yield Label(self.program_name, classes="program-name")
      yield Label(self._STATUS_LABELS.get(self._category, ""), classes="program-status")
      yield Label(self._mtime_text, classes="program-mtime")


class ProgramSelectionScreen(Screen[None]):
  """Landing screen listing programs connected to the log and/or command servers."""

  CSS_PATH = Path(__file__).with_suffix(".tcss")

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("r", "refresh", "Refresh"),
    ("f5", "refresh", ""),
  ]

  REFRESH_INTERVAL: ClassVar[float] = 10.0
  WAKE_WATCH_INTERVAL: ClassVar[float] = 1.0

  def __init__(self, log_root: Path, command_client: CommandClient) -> None:
    super().__init__()
    self._log_root = log_root
    self._command_client = command_client
    self._last_wake: str | None = None

  @override
  def compose(self) -> ComposeResult:
    yield Static("Select a program", id="selection-title")
    yield ListView(id="program-list")
    yield Footer()

  async def on_mount(self) -> None:
    self._last_wake = read_wake_token()
    await self._refresh()
    self.set_interval(self.REFRESH_INTERVAL, self._refresh)
    self.set_interval(self.WAKE_WATCH_INTERVAL, self._check_wake)

  async def _check_wake(self) -> None:
    """Refresh promptly when a command server pings the cross-process wake token."""
    token = read_wake_token()
    if token != self._last_wake:
      self._last_wake = token
      await self._refresh()

  async def _refresh(self) -> None:
    """Discover command servers, query log-server state, and rebuild the list."""
    await self._command_client.connect_all()
    connected = await self._query_connected_programs()
    file_programs = await asyncio.to_thread(self._scan_log_programs)
    command_servers = set(self._command_client.available_servers)

    names = set(file_programs) | connected | command_servers
    rows: list[ProgramRow] = []
    for name in sorted(names, key=str.lower):
      has_command = name in command_servers
      log_active = name in connected
      if has_command and log_active:
        category = "both"
      elif has_command:
        category = "command-only"
      else:
        category = "log-only"
      mtime = file_programs.get(name)
      mtime_text = datetime.fromtimestamp(mtime, tz=settings.tz).strftime("%Y-%m-%d %H:%M") if mtime is not None else "\u2014"
      rows.append(ProgramRow(name, category, mtime_text))

    await self._rebuild(rows)

  async def _query_connected_programs(self) -> set[str]:
    """Ask the live state-query socket which programs are currently sending logs."""
    try:
      reader, sock_writer = await asyncio.wait_for(
        asyncio.open_connection(settings.state_query_host, settings.state_query_port),
        timeout=0.5,
      )
      try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=0.5)
      finally:
        sock_writer.close()
        try:
          await asyncio.wait_for(sock_writer.wait_closed(), timeout=0.5)
        except OSError, TimeoutError:
          pass
      data: dict[str, object] = orjson.loads(raw)
      return set(data.get("connected_programs", []))  # type: ignore[arg-type]
    except OSError, TimeoutError, ValueError, KeyError:
      return set()

  def _scan_log_programs(self) -> dict[str, float | None]:
    """Map each program subfolder under the log root to its newest file mtime."""
    programs: dict[str, float | None] = {}
    try:
      with os.scandir(self._log_root) as it:
        for entry in it:
          if entry.is_dir(follow_symlinks=False):
            programs[entry.name] = _newest_mtime(Path(entry.path))
    except OSError:
      pass
    return programs

  async def _rebuild(self, rows: list[ProgramRow]) -> None:
    """Replace the list contents, preserving the highlighted program if possible."""
    list_view = self.query_one("#program-list", ListView)
    current: str | None = None
    if list_view.index is not None and 0 <= list_view.index < len(list_view.children):
      node = list_view.children[list_view.index]
      if isinstance(node, ProgramRow):
        current = node.program_name

    await list_view.clear()
    if not rows:
      await list_view.append(ListItem(Label("No connected programs or logs found.", classes="empty-message")))
      return

    new_index = 0
    for i, row in enumerate(rows):
      if row.program_name == current:
        new_index = i
      await list_view.append(row)
    list_view.index = new_index

  def on_list_view_selected(self, event: ListView.Selected) -> None:
    item = event.item
    if isinstance(item, ProgramRow):
      self.app.push_screen(ProgramScreen(item.program_name, self._log_root, self._command_client))

  async def action_refresh(self) -> None:
    await self._refresh()
