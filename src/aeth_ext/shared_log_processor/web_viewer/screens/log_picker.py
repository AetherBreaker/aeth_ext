# Standard library imports
from typing import TYPE_CHECKING, override

# Third party imports
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DirectoryTree, Footer, Header, Static

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from textual.app import ComposeResult


class FileChosen(Message):
  """Posted by file picker screen when a log file is selected."""

  def __init__(self, path: Path) -> None:
    super().__init__()
    self.path = path


class LogPickerScreen(Screen[None]):
  """Screen that lets the user browse and pick a log file."""

  def __init__(self, log_root: Path) -> None:
    super().__init__()
    self._log_root = log_root

  @override
  def compose(self) -> ComposeResult:
    yield Header(show_clock=True)
    yield Static("Select a log file to stream", id="picker-title")
    yield DirectoryTree(str(self._log_root), id="log-tree")
    yield Footer()

  def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
    selected = event.path
    if selected.suffix.lower() != ".txt":
      self.notify("Only .txt log files can be streamed", severity="warning")
      return
    self.post_message(FileChosen(selected))
