# Standard library imports
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, override

# Third party imports
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Footer, Input, Log, RichLog, Static

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from textual.app import ComposeResult


@dataclass
class _FindOptions:
  term: str = ""
  highlight: bool = True
  match_case: bool = False
  whole_word: bool = False
  use_regex: bool = False


class FindDialog(ModalScreen[_FindOptions | None]):
  """Find-in-page dialog."""

  DEFAULT_CSS = """
  FindDialog {
    align: center middle;
  }

  #find-dialog {
    background: $surface;
    border: thick $primary 60%;
    padding: 1 2;
    width: 58;
    height: auto;
  }

  #find-dialog-title {
    text-style: bold;
    margin-bottom: 1;
  }

  #find-input {
    margin-bottom: 1;
  }

  #find-options {
    margin-bottom: 1;
  }

  #find-buttons {
    align-horizontal: right;
  }

  #find-buttons Button {
    margin-left: 1;
  }
  """

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "cancel", "Cancel"),
  ]

  def __init__(self, current: _FindOptions | None = None) -> None:
    super().__init__()
    self._current = current or _FindOptions()

  @override
  def compose(self) -> ComposeResult:
    with Vertical(id="find-dialog"):
      yield Static("Find", id="find-dialog-title")
      yield Input(value=self._current.term, placeholder="Search…", id="find-input")
      with Vertical(id="find-options"):
        yield Checkbox("Highlight matches", value=self._current.highlight, id="cb-highlight")
        yield Checkbox("Match case", value=self._current.match_case, id="cb-case")
        yield Checkbox("Match whole words", value=self._current.whole_word, id="cb-word")
        yield Checkbox("Use regex", value=self._current.use_regex, id="cb-regex")
      with Horizontal(id="find-buttons"):
        yield Button("Find", variant="primary", id="btn-find")
        yield Button("Cancel", id="btn-cancel")

  def on_mount(self) -> None:
    self.query_one("#find-input", Input).focus()

  def action_cancel(self) -> None:
    self.dismiss(None)

  def on_button_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "btn-cancel":
      self.dismiss(None)
    elif event.button.id == "btn-find":
      self._submit()

  def on_input_submitted(self) -> None:
    self._submit()

  def _submit(self) -> None:
    term = self.query_one("#find-input", Input).value
    self.dismiss(
      _FindOptions(
        term=term,
        highlight=self.query_one("#cb-highlight", Checkbox).value,
        match_case=self.query_one("#cb-case", Checkbox).value,
        whole_word=self.query_one("#cb-word", Checkbox).value,
        use_regex=self.query_one("#cb-regex", Checkbox).value,
      )
    )


class LogStreamScreen(Screen[None]):
  """Screen that tails a single selected log file."""

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "back", "Back"),
    ("r", "refresh", "Reload"),
    ("ctrl+f", "find", "Find"),
  ]

  def __init__(self, log_path: Path) -> None:
    super().__init__()
    self._log_path = log_path
    self._cursor = 0
    self._last_signature: tuple[int, int] | None = None
    self._poll_timer = None
    self._lines: list[str] = []
    self._find_opts: _FindOptions | None = None

  @override
  def compose(self) -> ComposeResult:
    with Horizontal(id="stream-header"):
      yield Static("Streaming", classes="label")
      yield Static(str(self._log_path), id="stream-path")
    # yield Log(id="stream-log", auto_scroll=True, highlight=True)
    yield RichLog(id="stream-log", auto_scroll=True, highlight=True, markup=True)
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
    self._lines.clear()
    self._cursor = 0
    self._last_signature = None
    log_widget = self.query_one("#stream-log", RichLog)
    log_widget.clear()
    self._load_initial_tail()

  def action_find(self) -> None:
    def _on_result(opts: _FindOptions | None) -> None:
      if opts is None:
        return
      self._find_opts = opts if opts.term else None
      self._refresh_display()

    self.app.push_screen(FindDialog(self._find_opts), _on_result)

  # ---------------------------------------------------------------------------
  # Highlighting helpers
  # ---------------------------------------------------------------------------

  def _build_pattern(self, opts: _FindOptions) -> re.Pattern[str] | None:
    """Compile a search pattern from *opts*, returning None on error or empty term."""
    term = opts.term
    if not term:
      return None
    flags = 0 if opts.match_case else re.IGNORECASE
    if not opts.use_regex:
      term = re.escape(term)
    if opts.whole_word:
      term = rf"\b{term}\b"
    try:
      return re.compile(term, flags)
    except re.error:
      return None

  def _highlight_line(self, line: str) -> Text:
    """Return a Rich Text for *line*, with match spans highlighted if a find is active."""
    text = Text(line)
    if self._find_opts is None or not self._find_opts.highlight:
      return text
    pattern = self._build_pattern(self._find_opts)
    if pattern is None:
      return text
    for m in pattern.finditer(line):
      text.stylize("bold black on yellow", m.start(), m.end())
    return text

  def _refresh_display(self) -> None:
    """Clear the RichLog and re-render all stored lines with current highlight settings."""
    log_widget = self.query_one("#stream-log", RichLog)
    log_widget.clear()
    for line in self._lines:
      log_widget.write(self._highlight_line(line))

  # ---------------------------------------------------------------------------
  # File-reading helpers
  # ---------------------------------------------------------------------------

  def _load_initial_tail(self) -> None:
    log_widget = self.query_one("#stream-log", RichLog)
    if not self._log_path.exists():
      log_widget.write(f"File not found: {self._log_path}")
      return

    with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
      raw_lines = f.readlines()

    self._lines = [ln.rstrip("\n") for ln in raw_lines[-250:]]
    for line in self._lines:
      log_widget.write(self._highlight_line(line))

    stat = self._log_path.stat()
    self._cursor = stat.st_size
    self._last_signature = (stat.st_ino, stat.st_size)

  def _poll_file(self) -> None:
    if not self._log_path.exists():
      return

    stat = self._log_path.stat()
    signature = (stat.st_ino, stat.st_size)
    rolled = False
    if self._last_signature is not None:
      old_inode, old_size = self._last_signature
      if signature[0] != old_inode or stat.st_size < old_size:
        # File rolled over or truncated; restart stream from the beginning.
        self._cursor = 0
        self._lines.clear()
        rolled = True

    with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
      f.seek(self._cursor)
      chunk = f.read()
      self._cursor = f.tell()

    self._last_signature = signature

    if not chunk and not rolled:
      return

    new_lines = chunk.splitlines() if chunk else []
    self._lines.extend(new_lines)

    if rolled:
      self._refresh_display()
    else:
      log_widget = self.query_one("#stream-log", RichLog)
      for line in new_lines:
        log_widget.write(self._highlight_line(line))
