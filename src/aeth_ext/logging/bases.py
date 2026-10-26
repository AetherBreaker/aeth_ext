# pyright: reportIncompatibleMethodOverride=false
"""Custom log record, handlers, and formatters referenced by the packaged logging-config fragments."""

# Standard library imports
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
from aeth_ext.errors import alert
from aeth_ext.errors.send_alert_email import send_alert_email

# Re-exported: 9.x clients' remote configs name ``aeth_ext.logging.bases.SmartColumnFormatter`` to a 10.x server.
from aeth_ext.logging.column_formatter import SmartColumnFormatter
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
  """``RichHandler`` that renders a dotted module path instead of a bare filename, with ``settings.tz`` timestamps.

  The path is rooted at ``site-packages``, *project_name*, ``src``, or ``Lib``, checked in that order.
  """

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
    """Forward every ``RichHandler`` argument unchanged, keeping *project_name* for path rendering."""
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
  # Guards against unbounded recursion: parse_and_grab_constants() itself logs a
  # diagnostic message, and once the process's LogRecordFactory is TaggedLogRecord,
  # *every* logger call -- including that one -- constructs a TaggedLogRecord. Without
  # this flag, that nested construction would see _project_name still unset (the
  # outer call hasn't returned to cache it yet) and re-enter _resolve_project_name(),
  # recursing until RecursionError. Thread-local because the recursion only happens
  # on the same call stack; a genuinely concurrent resolution on another thread is
  # merely redundant work, not a bug.
  _resolving: ClassVar[threading.local] = threading.local()
  source_name: str | None
  record_id: int | None

  @classmethod
  def _project_name_pending(cls) -> bool:
    """Whether a ``"FIX_ME"`` result from ``_resolve_project_name`` is provisional rather than a real miss.

    Provisional for either of two reasons: a reentrant call on the same thread (see ``_resolving``),
    or a still-in-progress ``python -m pkg.submodule`` bootstrap -- true while a parent package is
    still being imported ahead of the target module becoming ``__main__``, e.g. that parent
    package's own ``__init__.py`` logging something as it loads. Gated on ``sys.argv[0] ==
    "-m"`` -- the placeholder the interpreter sets before ``runpy`` swaps ``__main__`` in, then
    overwrites with the resolved path once bootstrap finishes -- rather than merely "no
    ``__main__.__file__`` yet": this project only supports ``python -m``, a direct script, or a
    ``project.scripts`` entrypoint, all of which give ``__main__`` a real ``__file__`` immediately
    except for the ``-m`` bootstrap window itself. Without the ``-m`` gate, a genuinely file-less
    invocation (``python -c``, a REPL, an embedded interpreter -- none of which this project
    supports) would look pending forever and never raise. ``__init__`` uses this to decide whether
    ``"FIX_ME"`` means bootstrap isn't finished yet (tolerate, without caching, so a later record
    gets a real answer) or ``PROJECT_NAME`` is genuinely missing/the process deviated from a
    supported entrypoint (raise).
    """
    return getattr(cls._resolving, "active", False) or (
      sys.argv[:1] == ["-m"] and getattr(sys.modules.get("__main__"), "__file__", None) is None
    )

  @classmethod
  def _resolve_project_name(cls) -> str:
    """Resolve this process's ``PROJECT_NAME`` constant, once, and cache it.

    Deferred to first use rather than resolved at class-definition (module import) time: for
    ``python -m pkg.submodule`` invocations, this module can be first imported during runpy's
    dotted-path resolution, before ``sys.modules["__main__"]`` is swapped to the real entry
    module -- a class-definition-time lookup would see a bogus placeholder ``__main__`` (no
    ``__file__``) via ``get_entrypoint_root()``. That same window can still be open the first time
    this is actually called, though, if a parent package logs something during its own import (see
    ``_project_name_pending``) -- so a ``"FIX_ME"`` result is only ever cached once
    ``_project_name_pending()`` is false, letting a later, properly-bootstrapped call retry instead
    of being stuck with this one's premature miss.
    """
    project_name = cls._project_name
    if project_name is not None:
      return project_name
    if cls._project_name_pending():
      return "FIX_ME"
    cls._resolving.active = True
    try:
      consts = parse_and_grab_constants(expected_constants={"PROJECT_NAME": "project_name"})
    finally:
      cls._resolving.active = False
    project_name = consts.get("project_name", "FIX_ME")
    cls._project_name = project_name
    return project_name

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    """Tag the record with the project name and its ``libname``/``libpath`` before base-class init.

    A ``"FIX_ME"`` project name is tolerated only while ``_project_name_pending()`` holds; otherwise it raises.
    """
    self.source_name = None
    self.record_id = None
    self.project_name = TaggedLogRecord._resolve_project_name()
    if self.project_name == "FIX_ME" and not TaggedLogRecord._project_name_pending():
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
  """``Formatter`` whose ``formatTime`` renders in ``settings.tz`` instead of the process's local time."""

  default_msec_format = None

  @override
  def formatTime(self, record: TaggedLogRecord, datefmt: str | None = None) -> str:
    """Return the creation time of the specified LogRecord as formatted text.

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
  """``TimedRotatingFileHandler`` with tz-aware rollover, dated naming, a startup self-test, and a size watchdog.

  Rotated files are named ``<stem>.<date><suffix>``.

  The watchdog raises a low-priority alert when the live file reaches
  ``size_warn_bytes`` (default ``settings.log_file_size_warn_bytes``; 0 disables) and again at
  each doubling, so a runaway logger escalates without spamming; the ladder resets on rollover.
  The check is the stream position after each write -- no syscall, since append mode makes
  that the file size.
  """

  def __init__(self, *args: Any, size_warn_bytes: int | None = None, **kwargs: Any) -> None:
    """Initialise the base handler, arm the size watchdog at *size_warn_bytes*, and run the startup self-test."""
    super().__init__(*args, **kwargs)
    self._size_warn_bytes = BaseSettings.get_settings().log_file_size_warn_bytes if size_warn_bytes is None else size_warn_bytes
    self._next_size_warn = self._size_warn_bytes
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
    self._check_size()

  def _check_size(self) -> None:
    if self._size_warn_bytes <= 0 or self.stream is None:
      return
    try:
      size = self.stream.tell()
    except OSError, ValueError:
      return
    if size < self._next_size_warn:
      return
    threshold = self._next_size_warn
    while self._next_size_warn <= size:
      self._next_size_warn *= 2
    try:
      alert(
        "Log file growing unusually large",
        f"{self.baseFilename} has reached {size / 2**20:,.0f} MiB (threshold {threshold / 2**20:,.0f} MiB). "
        f"Next alert at {self._next_size_warn / 2**20:,.0f} MiB; the file rolls over at the next interval boundary as usual.",
        priority=-1,
        in_except_block=False,
        wait=False,  # runs inside `emit` under this handler's lock, which the send thread's own log calls would block on
      )
    except Exception:  # noqa: BLE001, S110 -- alerting must never take the write path down
      pass

  @override
  def getFilesToDelete(self) -> list[str]:
    """Rotated files beyond ``backupCount``, oldest first.

    The stdlib version only recognises ``<basename>.<date>``; ``doRollover`` below writes
    ``<stem>.<date><suffix>`` (``app.2026-08-27.log``), so it never matched anything and
    ``backupCount`` was silently a no-op. Only files with exactly this handler's stem, a
    date matching ``extMatch``, and this handler's suffix qualify -- ``app_debug.*`` is not
    ``app.*``.
    """
    base = Path(self.baseFilename)
    prefix = f"{base.stem}."
    suffix = base.suffix
    rotated: list[Path] = []
    for path in base.parent.iterdir():
      name = path.name
      if not (name.startswith(prefix) and name.endswith(suffix)) or name == base.name:
        continue
      stamp = name[len(prefix) : len(name) - len(suffix)]
      if self.extMatch.fullmatch(stamp) and path.is_file():
        rotated.append(path)
    if len(rotated) < self.backupCount:
      return []
    rotated.sort()
    return [str(p) for p in rotated[: len(rotated) - self.backupCount]]

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
    """Do a rollover; in this case, a date/time stamp is appended to the filename when the rollover happens.

    However, you want the file to be named for the start of the interval, not the current time.  If
    there is a backup count, then we have to get a list of matching filenames, sort them and remove
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
    self._next_size_warn = self._size_warn_bytes
