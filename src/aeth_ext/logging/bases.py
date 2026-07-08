# Standard library imports
import logging
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import gmtime, strftime, time
from typing import TYPE_CHECKING, Any, override

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
