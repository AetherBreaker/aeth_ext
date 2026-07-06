# Standard library imports
from typing import TYPE_CHECKING, ClassVar, override

# Third party imports
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Log, Static

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from textual.app import ComposeResult


class LogStreamScreen(Screen[None]):
  """Screen that tails a single selected log file."""

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "back", "Back"),
    ("r", "refresh", "Reload"),
  ]

  def __init__(self, log_path: Path) -> None:
    super().__init__()
    self._log_path = log_path
    self._cursor = 0
    self._last_signature: tuple[int, int] | None = None
    self._poll_timer = None

  @override
  def compose(self) -> ComposeResult:
    with Horizontal(id="stream-header"):
      yield Static("Streaming", classes="label")
      yield Static(str(self._log_path), id="stream-path")
    yield Log(id="stream-log", auto_scroll=True, highlight=True)
    yield Footer()

  def on_mount(self) -> None:
    self._load_initial_tail()
    self._poll_timer = self.set_interval(0.5, self._poll_file)

  def on_unmount(self) -> None:
    if self._poll_timer is not None:
      self._poll_timer.stop()

  def action_back(self) -> None:
    self.dismiss()

  def action_refresh(self) -> None:
    log_widget = self.query_one("#stream-log", Log)
    log_widget.clear()
    self._cursor = 0
    self._last_signature = None
    self._load_initial_tail()

  # @override
  # def action_scroll_left(self) -> None:
  #   """Scroll log view left."""
  #   log_widget = self.query_one("#stream-log", Log)
  #   log_widget.scroll_left()

  # @override
  # def action_scroll_right(self) -> None:
  #   """Scroll log view right."""
  #   log_widget = self.query_one("#stream-log", Log)
  #   log_widget.scroll_right()

  def _load_initial_tail(self) -> None:
    log_widget = self.query_one("#stream-log", Log)
    if not self._log_path.exists():
      log_widget.write_line(f"File not found: {self._log_path}")
      return

    with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
      lines = f.readlines()

    for line in lines[-250:]:
      log_widget.write_line(line.rstrip("\n"))

    stat = self._log_path.stat()
    self._cursor = stat.st_size
    self._last_signature = (stat.st_ino, stat.st_size)

  def _poll_file(self) -> None:
    log_widget = self.query_one("#stream-log", Log)
    if not self._log_path.exists():
      return

    stat = self._log_path.stat()
    signature = (stat.st_ino, stat.st_size)
    if self._last_signature is not None:
      old_inode, old_size = self._last_signature
      if signature[0] != old_inode or stat.st_size < old_size:
        # File rolled over or truncated; restart stream from the beginning.
        self._cursor = 0

    with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
      f.seek(self._cursor)
      chunk = f.read()
      self._cursor = f.tell()

    if chunk:
      for line in chunk.splitlines():
        log_widget.write_line(line)

    self._last_signature = signature
