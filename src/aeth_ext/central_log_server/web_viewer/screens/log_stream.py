# Standard library imports
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

# Third party imports
from rich.text import Text
from textual.containers import Horizontal, Middle, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, RichLog, Static
from watchfiles import awatch

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence

  # Third party imports
  from textual.app import ComposeResult
  from textual.binding import BindingType
  from textual.widget import Widget


@dataclass
class _FindOptions:
  term: str = ""
  highlight: bool = True
  match_case: bool = False
  whole_word: bool = False
  use_regex: bool = False


class _FindInput(Input):
  """Input that suppresses the focus-highlight border."""

  _PSEUDO_CLASSES: ClassVar[dict[str, Callable[[Widget], bool]]] = {
    **Input._PSEUDO_CLASSES,
    "focus": lambda self: False,
  }

  BINDINGS: ClassVar[list[BindingType]] = [*Input.BINDINGS, ("ctrl+backspace", "delete_word_left", "Delete word")]


class FindBar(Horizontal):
  """Inline find-bar that docks to the bottom of the screen."""

  @override
  def compose(self) -> ComposeResult:
    yield _FindInput(placeholder="Find in page…", id="find-input")
    with Middle(classes="cb-wrap"):
      yield Checkbox("Highlight All", value=True, id="cb-highlight", compact=True)
    with Middle(classes="cb-wrap"):
      yield Checkbox("Match Case", value=False, id="cb-case", compact=True)
    with Middle(classes="cb-wrap"):
      yield Checkbox("Whole Words", value=False, id="cb-word", compact=True)
    with Middle(classes="cb-wrap"):
      yield Checkbox("Regex", value=False, id="cb-regex", compact=True)
    yield Button("✕", id="find-close", variant="default")

  def on_mount(self) -> None:
    for cb in self.query(Checkbox):
      cb.can_focus = False
    btn = self.query_one("#find-close", Button)
    btn.can_focus = False

  def open(self) -> None:
    self.add_class("visible")
    self.query_one("#find-input", _FindInput).focus()

  def close(self) -> None:
    self.remove_class("visible")

  @property
  def is_open(self) -> bool:
    return "visible" in self.classes

  def current_opts(self) -> _FindOptions:
    return _FindOptions(
      term=self.query_one("#find-input", _FindInput).value,
      highlight=self.query_one("#cb-highlight", Checkbox).value,
      match_case=self.query_one("#cb-case", Checkbox).value,
      whole_word=self.query_one("#cb-word", Checkbox).value,
      use_regex=self.query_one("#cb-regex", Checkbox).value,
    )


class LogStreamScreen(Screen[None]):
  """Screen that tails a single selected log file."""

  CSS_PATH = Path(__file__).with_suffix(".tcss")

  BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
    ("escape", "back_or_close", "Back"),
    ("r", "refresh", "Reload"),
    ("ctrl+f", "find", "Find"),
  ]

  def __init__(self, log_path: Path) -> None:
    super().__init__()
    self._log_path = log_path
    self._cursor = 0
    self._last_signature: tuple[int, int] | None = None
    self._lines: list[str] = []
    self._find_opts: _FindOptions | None = None
    self._highlighter = None
    self._find_debounce_timer = None

  @override
  def compose(self) -> ComposeResult:
    with Horizontal(id="stream-header"):
      yield Static("Streaming", classes="label")
      yield Static(str(self._log_path), id="stream-path")
    yield Footer()
    with Vertical(id="log-body"):
      yield RichLog(id="stream-log", auto_scroll=True, highlight=True, markup=True)
      yield FindBar(id="find-bar")

  def on_mount(self) -> None:
    self._highlighter = self.query_one("#stream-log", RichLog).highlighter
    self._load_initial_tail()
    self.run_worker(self._watch_file_background_task(), exclusive=True, name="file-watcher")

  def action_back_or_close(self) -> None:
    bar = self.query_one("#find-bar", FindBar)
    if bar.is_open:
      bar.close()
      self._find_opts = None
      self._refresh_display()
    else:
      self.dismiss()

  def action_refresh(self) -> None:
    self._lines.clear()
    self._cursor = 0
    self._last_signature = None
    log_widget = self.query_one("#stream-log", RichLog)
    log_widget.clear()
    self._load_initial_tail()

  def action_find(self) -> None:
    bar = self.query_one("#find-bar", FindBar)
    if bar.is_open:
      bar.query_one("#find-input", _FindInput).focus()
    else:
      bar.open()

  # ------------------------------------------------------------------
  # FindBar event handlers
  # ------------------------------------------------------------------

  def on_input_changed(self, event: Input.Changed) -> None:
    if event.input.id == "find-input":
      self._apply_find()

  def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
    bar = self.query_one("#find-bar", FindBar)
    if bar.is_open:
      self._apply_find()

  def on_input_submitted(self, event: Input.Submitted) -> None:
    if event.input.id == "find-input":
      self._apply_find()

  def on_button_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "find-close":
      bar = self.query_one("#find-bar", FindBar)
      bar.close()
      self._find_opts = None
      self._refresh_display()

  def _apply_find(self) -> None:
    if self._find_debounce_timer is not None:
      self._find_debounce_timer.stop()
    self._find_debounce_timer = self.set_timer(0.12, self._apply_find_immediate)

  def _apply_find_immediate(self) -> None:
    self._find_debounce_timer = None
    opts = self.query_one("#find-bar", FindBar).current_opts()
    self._find_opts = opts if opts.term else None
    self._refresh_display()

  # ---------------------------------------------------------------------------
  # Highlighting helpers
  # ---------------------------------------------------------------------------

  def _build_pattern(self, opts: _FindOptions) -> re.Pattern[str] | None:
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
    if self._highlighter is not None:
      text = self._highlighter(line)
    else:
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
    log_widget = self.query_one("#stream-log", RichLog)
    with log_widget.app.batch_update():
      log_widget.clear()
      self._write_new_lines(self._lines, log_widget)

  def _write_new_lines(self, new_lines: Sequence[str], widget: RichLog) -> None:
    for line in new_lines:
      widget.write(self._highlight_line(line))

  # ---------------------------------------------------------------------------
  # File-reading helpers
  # ---------------------------------------------------------------------------

  async def _watch_file_background_task(self) -> None:
    """Worker: watch the log file with watchfiles and ingest new content on each change."""
    if not self._log_path.exists():
      return
    async for _ in awatch(self._log_path):
      self._poll_file()

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
        self._cursor = 0
        self._lines.clear()
        rolled = True

    with self._log_path.open("r", encoding="utf-8", errors="ignore") as f:
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
      self._write_new_lines(new_lines, log_widget)
