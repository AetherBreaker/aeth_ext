# pyright: reportIncompatibleMethodOverride=false
# Standard library imports
import hashlib
import json
import logging
import sys
import threading
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import strftime, time
from typing import TYPE_CHECKING, Any, ClassVar, override

# Third party imports
from rich.logging import RichHandler

# First party imports
from aeth_ext.errors.send_alert_email import send_alert_email
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
    record: TaggedLogRecord,
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


class TaggedLogRecord(logging.LogRecord):
  """A LogRecord with a ``name`` attribute that is always set to the logger's name.

  This is useful for log records received over a socket connection, where the
  logger's name may not be set correctly.
  """

  _project_name: ClassVar[str | None] = None
  source_name: str | None
  record_id: int | None

  @classmethod
  def _resolve_project_name(cls) -> str:
    """Resolve this process's ``PROJECT_NAME`` constant, once, and cache it.

    Deferred to first use rather than resolved at class-definition (module import) time: for
    ``python -m pkg.submodule`` invocations, this module can be first imported during runpy's
    dotted-path resolution, before ``sys.modules["__main__"]`` is swapped to the real entry
    module -- a class-definition-time lookup would see a bogus placeholder ``__main__`` (no
    ``__file__``) via ``get_entrypoint_root()`` and permanently cache ``"FIX_ME"`` for the rest of
    the process. By the time any real ``LogRecord`` is actually constructed, ``__main__`` is
    always the genuine entrypoint.
    """
    project_name = cls._project_name
    if project_name is None:
      consts = parse_and_grab_constants(expected_constants={"PROJECT_NAME": "project_name"})
      project_name = consts.get("project_name", "FIX_ME")
      cls._project_name = project_name
    return project_name

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    self.source_name = None
    self.record_id = None
    self.project_name = TaggedLogRecord._resolve_project_name()
    if self.project_name == "FIX_ME":
      raise ValueError("Expected project name to be set, but got 'FIX_ME'")
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
  def formatTime(self, record: TaggedLogRecord, datefmt: str | None = None) -> str:
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
  def formatTime(self, record: TaggedLogRecord, datefmt: str | None = None) -> str:
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
    if isinstance(handler, logging.FileHandler):
      hname: str = str(Path(handler.baseFilename).resolve())
    else:
      hname: str = handler.name or f"_anon_{id(handler)}"
    with self._lock:
      if hname not in self._widths_by_handler:
        self._widths_by_handler[hname] = [0] * len(self._columns)

    # Stashed directly on the handler (rather than only reachable via the
    # thread-local set inside ``_format_with_context``) so code outside of a
    # ``format()`` call -- e.g. the ``emit_separator`` monkey patch -- can
    # still look up which width tally belongs to this handler.
    handler._smart_col_handler_name = hname  # type: ignore[attr-defined]

    local = self._local
    original_format = handler.format

    def _format_with_context(record: TaggedLogRecord) -> str:
      local.handler_name = hname
      try:
        return original_format(record)
      finally:
        local.handler_name = None

    handler.format = _format_with_context  # pyright: ignore[reportAttributeAccessIssue]

  def _current_widths(self) -> list[int]:
    hname: str | None = getattr(self._local, "handler_name", None)
    if hname is not None:
      return self._widths_by_handler.get(hname, self._widths_default)
    return self._widths_default

  def format_separator(self, message: str, handler_name: str | None = None) -> str:
    """Render *message* centered in dashes, padded to match this handler's column width.

    Used by the ``Handler.emit_separator`` monkey patch (see
    ``aeth_ext.central_log_server.patches``) to print connect/disconnect rules
    that stay visually aligned with the surrounding columnar output. *handler_name*
    should be the same key ``_register_handler`` computed for the target handler
    (stashed as ``handler._smart_col_handler_name``); when omitted or unknown the
    shared default tally is used instead.

    Falls back to a fixed width of 60 when nothing has been tracked yet (e.g. the
    very first record a handler ever sees is a separator).
    """
    widths = self._widths_by_handler.get(handler_name, self._widths_default) if handler_name is not None else self._widths_default
    total_width = sum(widths[: self._n_tracked]) + len(self._separator) * self._n_tracked
    if total_width <= 0:
      total_width = 60
    return message.center(total_width, "-")

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

  def _render_columns(self, record: TaggedLogRecord) -> list[str]:
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
  def format(self, record: TaggedLogRecord) -> str:
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


# Extra key tagged onto any error :class:`~logging.LogRecord` that a
# :class:`CustomTimedRotatingFileHandler` emits about itself (a failure to
# write a record or to roll over). Handlers check incoming records for this
# extra before doing anything else: if present, the record originated from
# this handler class reporting its own failure, so it is routed to the
# e-mail alert system instead of being written/rolled-over normally, which
# would risk an infinite emit -> failure -> emit loop.
_SELF_ERROR_EXTRA = "_rotating_handler_self_error"

_MIDNIGHT = 24 * 60 * 60  # seconds in a day, mirrors logging.handlers._MIDNIGHT


class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
  def __init__(self, *args: Any, **kwargs: Any) -> None:
    super().__init__(*args, **kwargs)
    self._run_self_test()

  @override
  def emit(self, record: TaggedLogRecord) -> None:
    if getattr(record, _SELF_ERROR_EXTRA, False):
      # This record is this handler class reporting a failure about itself
      # (see handleError/_report_error below). Writing it through the normal
      # path could fail the same way and re-trigger this same branch forever,
      # so it is diverted straight to the e-mail alert system instead.
      self._alert_self_error(record)
      return
    super().emit(record)

  @override
  def handleError(self, record: TaggedLogRecord) -> None:
    """Report emit/rollover failures instead of the default stderr dump.

    Called by the base class whenever writing *record* or a rollover
    triggered while handling it raised an exception. Logs a new record
    tagged with :data:`_SELF_ERROR_EXTRA` so it can be recognized and
    e-mailed (rather than re-emitted normally) by any handler it reaches.
    """
    self._report_error(f"Failed to write or roll over log records for {self.baseFilename}", sys.exc_info())

  def _report_error(self, message: str, exc_info: tuple[Any, Any, Any] | None = None) -> None:
    logging.getLogger(__name__).error(
      message,
      exc_info=exc_info,
      extra={_SELF_ERROR_EXTRA: True},
    )

  def _alert_self_error(self, record: TaggedLogRecord) -> None:
    try:
      content = self.format(record)
    except Exception:  # noqa: BLE001
      content = record.getMessage()
    try:
      send_alert_email(f"Log rotation failure for {Path(self.baseFilename).name}", content)
    except Exception:  # noqa: BLE001, S110
      # Nothing else we can safely do here without risking another loop.
      pass

  def _run_self_test(self) -> None:
    """Verify this handler can write records and roll over successfully.

    Runs entirely against throwaway files placed alongside the real log
    file (never the real log file itself), so the existing log data can
    never be lost regardless of the outcome. Any artifact created by the
    test is removed afterwards.
    """
    base_path = Path(self.baseFilename)
    test_path = base_path.with_name(f".{base_path.name}.selftest")
    rotated_test_path = test_path.with_name(f"{test_path.name}.rollover")
    try:
      test_path.unlink(missing_ok=True)
      rotated_test_path.unlink(missing_ok=True)

      with test_path.open("w", encoding=self.encoding or "utf-8") as fh:
        fh.write("CustomTimedRotatingFileHandler self-test record\n")
        fh.flush()

      if not test_path.exists() or test_path.stat().st_size == 0:
        raise OSError(f"self-test write to {test_path} did not persist any data")

      # Exercise the same rename/rotate machinery doRollover relies on.
      self.rotate(str(test_path), str(rotated_test_path))

      if not rotated_test_path.exists():
        raise OSError(f"self-test rollover did not produce {rotated_test_path}")
      if test_path.exists():
        raise OSError(f"self-test rollover left a stale file at {test_path}")
    except Exception:  # noqa: BLE001
      self._report_error(f"Self-test failed for CustomTimedRotatingFileHandler at {self.baseFilename}", sys.exc_info())
    finally:
      test_path.unlink(missing_ok=True)
      rotated_test_path.unlink(missing_ok=True)

  def _weekday_offset(self, current_day: int) -> int:
    """Days to wait until ``self.dayOfWeek`` (0 = Monday), 0 if already on it."""
    day = current_day
    if day == self.dayOfWeek:
      return 0
    if day < self.dayOfWeek:
      return self.dayOfWeek - day
    return 6 - day + self.dayOfWeek + 1

  def _dst_adjustment(self, dst_now: Any, result: int) -> int:
    """Seconds to add/subtract to *result* to compensate for a DST change."""
    dst_at_rollover = datetime.fromtimestamp(result, tz=_tz).dst()
    if dst_now == dst_at_rollover:
      return 0
    if not dst_now:  # DST kicks in before next rollover, deduct an hour
      if not datetime.fromtimestamp(result - 3600, tz=_tz).dst():
        return 0
      return -3600
    return 3600  # DST bows out before next rollover, add an hour

  @override
  def computeRollover(self, currentTime: int) -> int:
    """Work out the rollover time based on the specified time.

    Identical to :meth:`TimedRotatingFileHandler.computeRollover` except
    midnight/weekly boundaries are always computed using ``SETTINGS.tz``
    rather than the process's local time or UTC, so rollovers line up with
    wall-clock midnight in the configured timezone regardless of what
    timezone the host machine is running in.
    """
    if self.when != "MIDNIGHT" and not self.when.startswith("W"):
      return currentTime + self.interval

    t = datetime.fromtimestamp(currentTime, tz=_tz)
    current_hour, current_minute, current_second = t.hour, t.minute, t.second
    current_day = t.weekday()  # 0 is Monday, matches time.struct_time.tm_wday

    if self.atTime is None:
      rotate_ts = _MIDNIGHT
    else:
      rotate_ts = (self.atTime.hour * 60 + self.atTime.minute) * 60 + self.atTime.second

    r = rotate_ts - ((current_hour * 60 + current_minute) * 60 + current_second)
    if r <= 0:
      r += _MIDNIGHT
      current_day = (current_day + 1) % 7
    result = currentTime + r

    if self.when.startswith("W"):
      result += self._weekday_offset(current_day) * _MIDNIGHT
      result += self.interval - _MIDNIGHT * 7
    else:
      result += self.interval - _MIDNIGHT

    result += self._dst_adjustment(t.dst(), result)
    return result

  @override
  def doRollover(self) -> None:
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
    time_tuple = datetime.fromtimestamp(t, tz=_tz).timetuple()
    dfn = base_path.with_name(self.rotation_filename(f"{base_path.stem}.{strftime(self.suffix, time_tuple)}{base_path.suffix}"))
    if dfn.exists():
      # The dated destination already exists (e.g. a previous process rolled
      # over before we got a chance to, or this handler was re-created mid-day
      # after a server restart and the interval's file was already written).
      # Advance rolloverAt so we don't retry the same stale dfn on every
      # subsequent emit - without this update shouldRollover() stays True
      # forever and rotation is permanently broken.
      self.rolloverAt = self.computeRollover(current_time)
      return

    if self.stream:
      self.stream.close()
      self.stream = None
    self.rotate(self.baseFilename, str(dfn))
    if self.backupCount > 0:
      for s in self.getFilesToDelete():
        Path(s).unlink()
    if not self.delay:
      self.stream = self._open()
    self.rolloverAt = self.computeRollover(current_time)
