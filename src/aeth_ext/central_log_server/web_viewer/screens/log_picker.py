# Standard library imports
import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, override

# Third party imports
import orjson
from rich.text import Text
from textual.containers import Grid
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DirectoryTree, Footer, Label, Static

# First party imports
from aeth_ext.central_log_server.settings import Settings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from rich.style import Style
  from textual.app import ComposeResult
  from textual.widgets._directory_tree import DirEntry
  from textual.widgets._tree import TreeNode


settings = Settings.get_settings()

_SHARED_LOG_DIR: Path = settings.persisted_dir_loc / "central_log_server"
_CLIENT_IDS_PATH: Path = _SHARED_LOG_DIR / "client_ids.json"
_MIDNIGHT_BASELINE_PATH: Path = _SHARED_LOG_DIR / "midnight_baseline.json"

# TODO Modify menu to alternatively prevent a view of "connected programs"
# TODO where you can then view a filtered directory tree of just files that belong to that connected program


# TODO Additionally add a protocol for sending "commands" to connected programs, where connected programs can register


class FileChosen(Message):
  """Posted by file picker screen when a log file is selected."""

  def __init__(self, path: Path) -> None:
    super().__init__()
    self.path = path


class ConfirmDeleteModal(ModalScreen[bool]):
  """Confirmation dialog before permanently deleting a log file."""

  DEFAULT_CSS = """
  ConfirmDeleteModal {
    align: center middle;
  }

  #confirm-dialog {
    grid-size: 2;
    grid-gutter: 1 2;
    grid-rows: 1fr 3;
    padding: 0 1;
    width: 60;
    height: 11;
    border: thick $background 80%;
    background: $surface;
  }

  #confirm-dialog > Label {
    column-span: 2;
    height: 1fr;
    width: 1fr;
    content-align: center middle;
  }

  #confirm-dialog Button {
    width: 100%;
  }
  """

  def __init__(self, path: Path) -> None:
    super().__init__()
    self._path = path

  @override
  def compose(self) -> ComposeResult:
    with Grid(id="confirm-dialog"):
      yield Label(f"Delete [bold]{self._path.name}[/bold]?\nThis cannot be undone.")
      yield Button("Delete", variant="error", id="delete-confirm")
      yield Button("Cancel", variant="primary", id="delete-cancel")

  def on_button_pressed(self, event: Button.Pressed) -> None:
    self.dismiss(event.button.id == "delete-confirm")


class LogFileTree(DirectoryTree):
  """DirectoryTree that renders per-file metadata columns inline on each row.

  Metadata columns (right of the filename):
    - Last-modified timestamp
    - Log ID increments written to this file today (since midnight)
    - File size in megabytes
  """

  METADATA_REFRESH_INTERVAL: ClassVar[float] = 10.0

  def __init__(self, log_root: Path, **kwargs: Any) -> None:
    super().__init__(log_root, **kwargs)
    self._log_root = log_root
    # Per-file record counts written since midnight, keyed by normcased
    # absolute path (matching the writer thread's handler.baseFilename key).
    self._file_records_since_midnight: dict[str, int] = {}

  @override
  def on_mount(self) -> None:
    self.run_worker(self.load_metadata())
    self.set_interval(self.METADATA_REFRESH_INTERVAL, self.load_metadata)

  async def load_metadata(self) -> None:
    """Refresh state from the live state-query socket, falling back to files."""
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
      self._file_records_since_midnight = data.get("file_records_since_midnight", {})  # type: ignore[assignment]
    except OSError, TimeoutError, ValueError, KeyError:
      # State server not reachable (dev mode, startup race, etc.).
      self._file_records_since_midnight = {}

    self.refresh()

  # Fixed display widths for each metadata column (chars).
  _COL_MTIME: ClassVar[int] = 16  # "YYYY-MM-DD HH:MM"
  _COL_IDS: ClassVar[int] = 11  # "  9999 IDs"
  _COL_SIZE: ClassVar[int] = 10  # " 999.99 MB"

  @override
  def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
    label = super().render_label(node, base_style, style)

    if node.data is None or not node.data.path.is_file():
      return label

    path = node.data.path

    # File stats
    try:
      st = path.stat()
      mtime = datetime.fromtimestamp(st.st_mtime, tz=settings.tz).strftime("%Y-%m-%d %H:%M")
      size_mb = f"{st.st_size / 1_048_576:.2f} MB"
    except OSError:
      mtime = "—" * self._COL_MTIME
      size_mb = "—"

    # IDs written to this file today (keyed by normcased absolute path).
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    ids_today = self._file_records_since_midnight.get(key, 0)

    # Build fixed-width metadata suffix so every row's columns line up.
    # Each column is padded/truncated to a constant display width.

    suffix = Text()
    suffix.append(f" {mtime:<{self._COL_MTIME}}", style="dim")
    suffix.append(f" {ids_today:>{self._COL_IDS - 5}} IDs", style="dim")
    suffix.append(f" {size_mb:>{self._COL_SIZE - 1}}", style="dim")

    # Pad between the filename label and the right-docked suffix.
    # The tree prepends guide/indent characters outside of render_label, so we
    # must subtract that indent width from the available space ourselves.
    # Each depth level occupies exactly self.guide_depth columns.
    total_width = self.size.width
    depth = 0
    _n = node
    while _n.parent is not None:
      depth += 1
      _n = _n.parent
    indent_width = depth * self.guide_depth
    used = indent_width + label.cell_len + suffix.cell_len
    padding = max(1, total_width - used)
    label.append(" " * padding)
    # label.append(" " * padding)
    label.append_text(suffix)

    return label


class LogPickerScreen(Screen[None]):
  """Screen that lets the user browse and pick a log file."""

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("r", "refresh_tree", "Refresh"),
    ("f5", "refresh_tree", ""),
    ("d", "delete_file", "Delete File"),
  ]

  def __init__(self, log_root: Path) -> None:
    super().__init__()
    self._log_root = log_root

  @override
  def compose(self) -> ComposeResult:
    yield Static("Select a log file to stream", id="picker-title")
    yield LogFileTree(self._log_root, id="log-tree")
    yield Footer()

  def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
    selected = event.path
    if selected.suffix.lower() != ".txt":
      self.notify("Only .txt log files can be streamed", severity="warning")
      return
    self.post_message(FileChosen(selected))

  async def action_refresh_tree(self) -> None:
    """Reload metadata cache and rescan the directory tree."""
    tree = self.query_one(LogFileTree)
    await tree.load_metadata()
    await tree.reload()

  async def action_delete_file(self) -> None:
    """Prompt for confirmation then delete the currently highlighted log file."""
    tree = self.query_one(LogFileTree)
    node = tree.cursor_node
    if node is None or node.data is None or not node.data.path.is_file():
      self.notify("Highlight a file first", severity="warning")
      return

    path = node.data.path
    confirmed: bool = await self.app.push_screen_wait(ConfirmDeleteModal(path))
    if not confirmed:
      return

    try:
      path.unlink()
      self.notify(f"Deleted {path.name}")
      await tree.reload()
    except OSError as exc:
      self.notify(f"Delete failed: {exc}", severity="error")
