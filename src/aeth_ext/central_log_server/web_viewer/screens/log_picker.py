# Standard library imports
import asyncio
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, override

# Third party imports
import orjson
from rich.text import Text
from textual import work
from textual.containers import Grid
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DirectoryTree, Footer, Label, Static

# First party imports
from aeth_ext.central_log_server.protocol import LENGTH_STRUCT
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
    - Program name, highlighted green if connected to the server or red if not
    - Log ID increments received today (since midnight)
    - File size in megabytes
  """

  METADATA_RECONNECT_MAX: ClassVar[float] = 10.0

  def __init__(self, log_root: Path, **kwargs: Any) -> None:
    super().__init__(log_root, **kwargs)
    self._log_root = log_root
    self._connected_programs: set[str] = set()
    self._current_ids: dict[str, int] = {}
    self._midnight_ids: dict[str, int] = {}

  def _refresh_labels(self) -> None:
    """Clear Tree's line cache so render_label is re-invoked on next repaint.

    Textual's Tree widget caches each rendered row keyed by
    ``(node_id, is_selected, is_hover, is_cursor)``.  External state changes
    such as a program connecting or disconnecting are invisible to that key,
    so ``refresh()`` alone returns the stale cached Strip.  ``_clear_line_cache``
    is the semi-private method Textual itself uses for the same purpose (e.g.
    on scroll and on cursor move) and is safe to call from a subclass.
    """
    self._clear_line_cache()
    self.refresh()

  @override
  def on_mount(self) -> None:
    # Seed from disk so the tree renders immediately, then keep it live via a
    # single persistent push connection to the writer thread.
    self._load_metadata_from_files()
    self._refresh_labels()
    self.run_worker(self._subscriber_loop(), exclusive=True)

  async def _subscriber_loop(self) -> None:
    """Hold a persistent push connection, applying snapshots/events live.

    The writer thread pushes a full stats snapshot on connect, an immediate
    event on every client connect/disconnect (so the viewer reflects those
    instantly), and a periodic stats refresh at its idle cadence.  On any
    socket failure the on-disk files are used as a fallback and the connection
    is retried with a capped exponential backoff.
    """
    backoff = 1.0
    while True:
      try:
        reader, sock_writer = await asyncio.open_connection(
          settings.state_push_host,
          settings.state_push_port,
        )
      except OSError:
        self._load_metadata_from_files()
        self._refresh_labels()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, self.METADATA_RECONNECT_MAX)
        continue

      backoff = 1.0
      try:
        while True:
          header = await reader.readexactly(LENGTH_STRUCT.size)
          (length,) = LENGTH_STRUCT.unpack(header)
          self._apply_packet(orjson.loads(await reader.readexactly(length)))
          self._refresh_labels()
      except OSError, asyncio.IncompleteReadError, ValueError:
        self._load_metadata_from_files()
        self._refresh_labels()
      finally:
        sock_writer.close()
        with suppress(OSError):
          await sock_writer.wait_closed()

  def _apply_packet(self, packet: dict[str, Any]) -> None:
    """Apply a pushed ``stats`` snapshot or a ``connected``/``disconnected`` event.

    Both wire shapes carry the same state fields: a ``stats`` packet holds them
    at the top level, while an ``event`` packet nests them under ``snapshot``.
    """
    data: dict[str, Any] = packet.get("snapshot", packet)
    self._connected_programs = set(data.get("connected_programs", []))
    self._current_ids = data.get("current_ids", {})
    today_str = datetime.now(tz=settings.tz).date().isoformat()
    if data.get("midnight_date") == today_str:
      self._midnight_ids = data.get("midnight_ids", {})
    else:
      self._midnight_ids = {}

  def _load_metadata_from_files(self) -> None:
    """Fallback metadata reader that parses the on-disk JSON files."""
    # Connected programs are only tracked in memory via the state server;
    # the file is no longer written, so fall back to empty on disconnect.
    self._connected_programs = set()

    # Current record IDs per program
    try:
      raw_ids: dict[str, dict[str, object]] = orjson.loads(_CLIENT_IDS_PATH.read_bytes())
      self._current_ids = {
        name: int(entry["last_record_id"])  # type: ignore[arg-type]
        for name, entry in raw_ids.items()
      }
    except OSError, ValueError, TypeError, KeyError:
      self._current_ids = {}

    # Midnight baseline IDs (IDs at the start of today)
    today_str = datetime.now(tz=settings.tz).date().isoformat()
    try:
      raw_midnight: dict[str, object] = orjson.loads(_MIDNIGHT_BASELINE_PATH.read_bytes())
      if raw_midnight.get("date") == today_str:
        self._midnight_ids = {k: int(v) for k, v in raw_midnight.items() if k != "date"}  # type: ignore[arg-type]
      else:
        self._midnight_ids = {}
    except OSError, ValueError, TypeError:
      self._midnight_ids = {}

  # Fixed display widths for each metadata column (chars).
  _COL_MTIME: ClassVar[int] = 16  # "YYYY-MM-DD HH:MM"
  _COL_PROG: ClassVar[int] = 27  # " <name padded to 25> " with 1-space margins
  _COL_IDS: ClassVar[int] = 11  # "  9999 IDs"
  _COL_SIZE: ClassVar[int] = 10  # " 999.99 MB"

  @override
  def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
    label = super().render_label(node, base_style, style)

    if node.data is None or not node.data.path.is_file():
      return label

    path = node.data.path
    # Derive the program name from the first subfolder under log_root.
    try:
      rel = path.relative_to(self._log_root)
      program_name = rel.parts[0] if len(rel.parts) > 1 else ""
    except ValueError:
      program_name = ""

    # File stats
    try:
      st = path.stat()
      mtime = datetime.fromtimestamp(st.st_mtime, tz=settings.tz).strftime("%Y-%m-%d %H:%M")
      size_mb = f"{st.st_size / 1_048_576:.2f} MB"
    except OSError:
      mtime = "—" * self._COL_MTIME
      size_mb = "—"

    # IDs received today
    current_id = self._current_ids.get(program_name, 0)
    midnight_id = self._midnight_ids.get(program_name, 0)
    ids_today = max(0, current_id - midnight_id)

    # Connection-status colour for the program name badge
    if program_name:
      is_connected = program_name in self._connected_programs
      prog_style = "on dark_green" if is_connected else "on dark_red"
    else:
      prog_style = "dim"

    # Build fixed-width metadata suffix so every row's columns line up.
    # Each column is padded/truncated to a constant display width.

    suffix = Text()
    suffix.append(f" {mtime:<{self._COL_MTIME}}", style="dim")
    if program_name:
      prog_text = f" {program_name[: self._COL_PROG - 2]:<{self._COL_PROG - 2}} "
      suffix.append(" ")
      suffix.append(prog_text, style=prog_style)
    else:
      suffix.append(" " * (self._COL_PROG + 1), style="dim")
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
    if selected.suffix.lower() not in (".txt", ".log"):
      self.notify("Only .txt or .log log files can be streamed", severity="warning")
      return
    self.post_message(FileChosen(selected))

  async def action_refresh_tree(self) -> None:
    """Rescan the directory tree; state columns stay live via the push feed."""
    tree = self.query_one(LogFileTree)
    await tree.reload()

  @work
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
