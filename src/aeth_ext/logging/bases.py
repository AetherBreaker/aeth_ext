# Standard library imports
import hashlib
import json
import logging
import threading
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import gmtime, strftime, time
from typing import TYPE_CHECKING, Any, ClassVar, override

# Third party imports
from rich.logging import RichHandler

# First party imports
from aeth_ext.settings import BaseSettings
from aeth_ext.static_eval import parse_and_grab_constants

_tz = BaseSettings.get_settings().tz

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterable
  from types import ModuleType

  # Third party imports
  from rich._log_render import FormatTimeCallable
  from rich.console import Console, ConsoleRenderable
  from rich.highlighter import Highlighter
  from rich.traceback import Traceback

__all__ = [
  "CustomTimedRotatingFileHandler",
  "FixedFormatter",
  "FixedRichHandler",
  "SmartColumnFormatter",
  "TaggedLogRecord",
]


class FixedRichHandler(RichHandler):
  def __init__(
    self,
    level: int | str = logging.NOTSET,
    console: Console | None = None,
    *,
    show_time: bool = True,
    omit_repeated_times: bool = True,
    show_level: bool = True,
    show_path: bool = True,
    enable_link_path: bool = True,
    highlighter: Highlighter | None = None,
    markup: bool = False,
    rich_tracebacks: bool = False,
    tracebacks_width: int | None = None,
    tracebacks_code_width: int | None = 88,
    tracebacks_extra_lines: int = 3,
    tracebacks_theme: str | None = None,
    tracebacks_word_wrap: bool = True,
    tracebacks_show_locals: bool = False,
    tracebacks_suppress: Iterable[str | ModuleType] = (),
    tracebacks_max_frames: int = 100,
    locals_max_length: int = 10,
    locals_max_string: int = 80,
    log_time_format: str | FormatTimeCallable = "[%x %X]",
    keywords: list[str] | None = None,
    project_name: str | None = None,
  ) -> None:
    self.project_name = project_name
    super().__init__(
      level=level,
      console=console,
      show_time=show_time,
      omit_repeated_times=omit_repeated_times,
      show_level=show_level,
      show_path=show_path,
      enable_link_path=enable_link_path,
      highlighter=highlighter,
      markup=markup,
      rich_tracebacks=rich_tracebacks,
      tracebacks_width=tracebacks_width,
      tracebacks_code_width=tracebacks_code_width,
      tracebacks_extra_lines=tracebacks_extra_lines,
      tracebacks_theme=tracebacks_theme,
      tracebacks_word_wrap=tracebacks_word_wrap,
      tracebacks_show_locals=tracebacks_show_locals,
      tracebacks_suppress=tracebacks_suppress,
      tracebacks_max_frames=tracebacks_max_frames,
      locals_max_length=locals_max_length,
      locals_max_string=locals_max_string,
      log_time_format=log_time_format,
      keywords=keywords,
    )

  @override
  def render(
    self,
    *,
    record: logging.LogRecord,
    traceback: Traceback | None,
    message_renderable: ConsoleRenderable,
  ) -> ConsoleRenderable:
    """Render log for display.

    Args:
        record (LogRecord): logging Record.
        traceback (Traceback | None): Traceback instance or None for no Traceback.
        message_renderable (ConsoleRenderable): Renderable (typically Text) containing log message contents.

    Returns:
        ConsoleRenderable: Renderable to display log.
    """

    pathpath = Path(record.pathname)

    if "site-packages" in pathpath.parts:
      libname_index = pathpath.parts.index("site-packages") + 1
    elif self.project_name in pathpath.parts:
      libname_index = pathpath.parts.index(self.project_name)
    elif "src" in pathpath.parts:
      libname_index = pathpath.parts.index("src")
    elif "Lib" in pathpath.parts:
      libname_index = pathpath.parts.index("Lib") + 1
    else:
      libname_index = 0

    path = ".".join(pathpath.parts[libname_index:])
    if "src." in path:
      path = path.split("src.", 1)[1]

    level = self.get_level_text(record)
    time_format = None if self.formatter is None else self.formatter.datefmt
    log_time = datetime.fromtimestamp(record.created, tz=_tz)

    return self._log_render(
      self.console,
      [message_renderable, traceback] if traceback else [message_renderable],
      log_time=log_time,
      time_format=time_format,
      level=level,
      path=path,
      line_no=record.lineno,
      link_path=record.pathname if self.enable_link_path else None,
    )


expected_consts = parse_and_grab_constants(expected_constants={"PROJECT_NAME": "project_name"})


class TaggedLogRecord(logging.LogRecord):
  """A LogRecord with a ``name`` attribute that is always set to the logger's name.

  This is useful for log records received over a socket connection, where the
  logger's name may not be set correctly.
  """

  _PROJECT_NAME: str = expected_consts.get("project_name", "FIX_ME")
  source_name: str | None
  record_id: int | None

  def __init__(self, *args: Any, **kwargs: Any):
    self.source_name = None
    self.record_id = None
    self.project_name = TaggedLogRecord._PROJECT_NAME
    self.source_path = Path(args[2])
    parts = self.source_path.parts

    if "site-packages" in parts:
      libname_index = parts.index("site-packages") + 1
    elif self.project_name in parts:
      libname_index = parts.index(self.project_name)
    elif "src" in parts:
      libname_index = parts.index("src")
    elif "Lib" in parts:
      libname_index = parts.index("Lib") + 1
    else:
      libname_index = None

    libpath = ".".join(parts[libname_index or 0 :])

    if "src." in libpath:
      libpath = libpath.split("src.", 1)[1]

    self.libname = parts[libname_index] if libname_index is not None else self.project_name
    self.libpath = libpath

    super().__init__(*args, **kwargs)


class FixedFormatter(logging.Formatter):
  default_msec_format = None

  @override
  def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
    """
    Return the creation time of the specified LogRecord as formatted text.

    This method should be called from format() by a formatter which
    wants to make use of a formatted time. This method can be overridden
    in formatters to provide for any specific requirement, but the
    basic behaviour is as follows: if datefmt (a string) is specified,
    it is used with time.strftime() to format the creation time of the
    record. Otherwise, an ISO8601-like (or RFC 3339-like) format is used.
    The resulting string is returned. This function uses a user-configurable
    function to convert the creation time to a tuple. By default,
    time.localtime() is used; to change this for a particular formatter
    instance, set the 'converter' attribute to a function with the same
    signature as time.localtime() or time.gmtime(). To change it for all
    formatters, for example if you want all logging times to be shown in GMT,
    set the 'converter' attribute in the Formatter class.
    """
    dt = datetime.fromtimestamp(record.created, tz=_tz)
    if datefmt:
      s = dt.strftime(datefmt)
    else:
      s = dt.strftime(self.default_time_format)
      if self.default_msec_format:
        s = self.default_msec_format % (s, record.msecs)
    return s


_UNSET: object = object()


class _DefaultMap:
  """Mapping that returns ``""`` for any key absent from the wrapped dict.

  Used with :func:`str.format_map` so column templates referencing attributes
  not present on a :class:`~logging.LogRecord` produce ``""`` instead of
  raising :exc:`KeyError`.
  """

  __slots__ = ("_d",)

  def __init__(self, d: dict[str, Any]) -> None:
    self._d = d

  def __getitem__(self, key: str) -> Any:
    return self._d.get(key, "")


class SmartColumnFormatter(logging.Formatter):
  """A :class:`~logging.Formatter` that renders records as dynamically aligned columns.

  Each column is defined by a ``{}``-style template string (e.g. ``"{asctime}"``,
  ``"{levelname: >8}"``, ``"{message}"``) referencing any attribute of a
  :class:`~logging.LogRecord`.

  **Alignment**: the formatter tracks the widest rendered value seen for each
  tracked column and pads all values with spaces so that columns stay aligned
  as new records arrive.  Tracked widths can be persisted to a JSON file so
  alignment is preserved across restarts.

  **Per-handler tracking**: when the formatter is attached to a handler via
  :meth:`logging.Handler.setFormatter`, it wraps that handler's ``format``
  method so each handler maintains an **independent** width tally.  A single
  formatter instance can therefore be shared across multiple handlers (e.g.
  ``debug_file`` and ``info_file``) and each log file stays as narrow as its
  own content allows.

  **Tracked columns**: all columns except the last are tracked by default.
  If *right_align_last* is :data:`True` the last column is also tracked and
  its content is right-justified to the running maximum width.

  **Multiline values**: when any column value contains newline characters the
  output spans multiple rows.  All columns' lines are zipped together
  row-by-row; columns that exhaust their lines early show blank padding on
  continuation rows.

  Args:
    columns: Ordered list of ``{}``-style format strings, one per column.
    separator: String inserted between adjacent columns (default ``" | "``).
    persist_path: JSON file used to persist maximum column widths across
      process restarts.  The default (sentinel :data:`_UNSET`) resolves at
      construction time to
      ``settings.persisted_dir_loc / "logging_column_widths.json"``.
      Pass :data:`None` to disable persistence.
    right_align_last: When :data:`True` the last column is also tracked and
      its content is right-justified to the running maximum width.
    datefmt: Date/time format string forwarded to :meth:`formatTime`.
  """

  default_msec_format = None
  _file_lock: ClassVar[threading.Lock] = threading.Lock()

  def __init__(
    self,
    columns: list[str],
    separator: str = " | ",
    persist_path: Path | None = _UNSET,  # type: ignore[assignment]
    *,
    right_align_last: bool = False,
    datefmt: str | None = None,
  ) -> None:
    super().__init__(datefmt=datefmt)
    if persist_path is _UNSET:
      persist_path = BaseSettings.get_settings().persisted_dir_loc / "logging_column_widths.json"
    self._columns = columns
    self._separator = separator
    self._persist_path: Path | None = persist_path
    self._right_align_last = right_align_last
    self._n_tracked = len(columns) if right_align_last else len(columns) - 1
    self._widths_default: list[int] = [0] * len(columns)
    self._widths_by_handler: dict[str, list[int]] = {}
    self._lock = threading.Lock()
    self._local = threading.local()
    key_src = separator + "||" + "||".join(columns)
    self._key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
    self._load_all_widths()

  @override
  def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
    dt = datetime.fromtimestamp(record.created, tz=_tz)
    if datefmt:
      s = dt.strftime(datefmt)
    else:
      s = dt.strftime(self.default_time_format)
      if self.default_msec_format:
        s = self.default_msec_format % (s, record.msecs)
    return s

  def _register_handler(self, handler: logging.Handler) -> None:
    """Attach this formatter to *handler*, giving it a dedicated width tally.

    Called automatically by the :func:`logging.Handler.setFormatter` patch
    installed at module level.  Wraps ``handler.format`` with a closure that
    sets a thread-local ``handler_name`` before delegating to the original
    method, allowing :meth:`_current_widths` to select the right tally.
    """
    hname: str = handler.name or f"_anon_{id(handler)}"
    with self._lock:
      if hname not in self._widths_by_handler:
        self._widths_by_handler[hname] = [0] * len(self._columns)

    local = self._local
    original_format = handler.format

    def _format_with_context(record: logging.LogRecord) -> str:
      local.handler_name = hname
      try:
        return original_format(record)
      finally:
        local.handler_name = None

    handler.format = _format_with_context

  def _current_widths(self) -> list[int]:
    hname: str | None = getattr(self._local, "handler_name", None)
    if hname is not None:
      return self._widths_by_handler.get(hname, self._widths_default)
    return self._widths_default

  def _load_all_widths(self) -> None:
    if self._persist_path is None:
      return
    try:
      with SmartColumnFormatter._file_lock:
        raw = self._persist_path.read_text(encoding="utf-8")
        data: dict[str, dict[str, list[int]]] = json.loads(raw)
      section = data.get(self._key, {})
      n = len(self._columns)
      for hname, saved in section.items():
        widths = [0] * n
        for i, w in enumerate(saved):
          if i < n:
            widths[i] = max(0, w)
        if hname == "__default__":
          self._widths_default = widths
        else:
          self._widths_by_handler[hname] = widths
    except OSError, json.JSONDecodeError, ValueError, TypeError:
      pass

  def _save_widths(self) -> None:
    if self._persist_path is None:
      return
    with self._lock:
      snapshot: dict[str, list[int]] = {
        "__default__": list(self._widths_default),
        **{k: list(v) for k, v in self._widths_by_handler.items()},
      }
    try:
      with SmartColumnFormatter._file_lock:
        try:
          data: dict[str, dict[str, list[int]]] = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
          data = {}
        data[self._key] = snapshot
        self._persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
      pass

  def _update_width(self, col_idx: int, width: int) -> bool:
    widths = self._current_widths()
    with self._lock:
      if width > widths[col_idx]:
        widths[col_idx] = width
        return True
    return False

  def _render_columns(self, record: logging.LogRecord) -> list[str]:
    dm = _DefaultMap(record.__dict__)
    rendered = [col.format_map(dm) for col in self._columns]
    if record.exc_text:
      rendered[-1] += ("\n" if rendered[-1] else "") + record.exc_text.rstrip()
    if record.stack_info:
      rendered[-1] += ("\n" if rendered[-1] else "") + self.formatStack(record.stack_info)
    return rendered

  def _track_widths(self, col_lines: list[list[str]]) -> None:
    changed = False
    for i in range(self._n_tracked):
      max_w = max(len(line) for line in col_lines[i])
      if self._update_width(i, max_w):
        changed = True
    if self._right_align_last:
      last = len(self._columns) - 1
      max_w = max(len(line) for line in col_lines[last])
      if self._update_width(last, max_w):
        changed = True
    if changed:
      self._save_widths()

  def _pad_cell(self, c: int, cell: str, widths: list[int]) -> str:
    if c == len(self._columns) - 1:
      if self._right_align_last:
        return cell.rjust(widths[c])
      return cell
    if c < self._n_tracked:
      return cell.ljust(widths[c])
    return cell

  @override
  def format(self, record: logging.LogRecord) -> str:
    record.message = record.getMessage()
    record.asctime = self.formatTime(record, self.datefmt)
    if record.exc_info and not record.exc_text:
      record.exc_text = self.formatException(record.exc_info)

    col_lines: list[list[str]] = [r.split("\n") for r in self._render_columns(record)]
    self._track_widths(col_lines)
    widths = self._current_widths()

    max_rows = max(len(lines) for lines in col_lines)
    output_rows: list[str] = []
    for row_idx in range(max_rows):
      parts = [self._pad_cell(c, lines[row_idx] if row_idx < len(lines) else "", widths) for c, lines in enumerate(col_lines)]
      output_rows.append(self._separator.join(parts))
    return "\n".join(output_rows)


_orig_handler_set_formatter = logging.Handler.setFormatter


def _patched_set_formatter(self: logging.Handler, fmt: logging.Formatter | None) -> None:
  _orig_handler_set_formatter(self, fmt)
  if fmt is not None and hasattr(fmt, "_register_handler"):
    fmt._register_handler(self)  # type: ignore[union-attr]


logging.Handler.setFormatter = _patched_set_formatter


class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
  @override
  def doRollover(self):
    """
    do a rollover; in this case, a date/time stamp is appended to the filename
    when the rollover happens.  However, you want the file to be named for the
    start of the interval, not the current time.  If there is a backup count,
    then we have to get a list of matching filenames, sort them and remove
    the one with the oldest suffix.
    """
    base_path = Path(self.baseFilename)
    # get the time that this sequence started at and make it a TimeTuple
    current_time = int(time())
    t = self.rolloverAt - self.interval
    if self.utc:
      time_tuple = gmtime(t)
    else:
      time_tuple = datetime.fromtimestamp(t, tz=_tz).timetuple()
    dfn = base_path.with_name(self.rotation_filename(f"{base_path.stem}.{strftime(self.suffix, time_tuple)}{base_path.suffix}"))
    if dfn.exists():
      # Already rolled over.
      return

    if self.stream:
      self.stream.close()
      self.stream = None  # type: ignore
    self.rotate(self.baseFilename, str(dfn))
    if self.backupCount > 0:
      for s in self.getFilesToDelete():
        Path(s).unlink()
    if not self.delay:
      self.stream = self._open()
    self.rolloverAt = self.computeRollover(current_time)
