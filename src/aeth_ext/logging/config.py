# Standard library imports
import logging
from annotationlib import Format
from atexit import register
from concurrent.interpreters import get_current, get_main
from inspect import signature
from itertools import chain
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import Queue
from sys import platform
from typing import TYPE_CHECKING, Any, Literal

# Third party imports
from rich.traceback import install

# First party imports
from aeth_ext.logging.bases import FixedFormatter, FixedRichHandler, NamedLogRecord
from aeth_ext.settings import BaseSettings
from aeth_ext.types.abc import CapturesSubclasses

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from concurrent.interpreters import Queue as InterpreterQueue
  from multiprocessing import Queue as ProcessQueue
  from queue import Queue as ThreadQueue

  # Third party imports
  from aiologic import Queue as AioQueue
  from rich.console import Console

  # First party imports
  from aeth_ext.shared_log_processor.protocol import HandlerDef
  from aeth_ext.shared_log_processor.server.dispatch import WriterItem


if get_current() == get_main():
  settings = BaseSettings.get_settings()


__all__ = [
  "BaseLoggingConfig",
  "QueueCatchall",
  "get_preferred_logrecord_formatter",
  "set_preferred_logrecord_formatter",
]

type RootLogger = logging.Logger
type QueueCatchall = InterpreterQueue | ProcessQueue[NamedLogRecord] | ThreadQueue[NamedLogRecord]

__preferred_file_formatter: FixedFormatter | None = None
__DEFAULT_MAX_WIDTH = 36
__DEFAULT_TIMESTAMP_FORMAT = "%b, %d %a %I:%M %p"


def get_preferred_logrecord_formatter(default_max_width: int | None = None, timestamp_format: str | None = None) -> FixedFormatter:
  global __preferred_file_formatter
  if __preferred_file_formatter is None:
    __preferred_file_formatter = FixedFormatter(
      fmt=f"{{libpath: <{default_max_width or __DEFAULT_MAX_WIDTH}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}",
      datefmt=timestamp_format or __DEFAULT_TIMESTAMP_FORMAT,
      style="{",
    )
  return __preferred_file_formatter


def set_preferred_logrecord_formatter(formatter: FixedFormatter) -> None:
  global __preferred_file_formatter
  __preferred_file_formatter = formatter


class BaseLoggingConfig(CapturesSubclasses):
  """
  In order to modify logging configuration, you can subclass this class and override the methods.
  Call super().method() if extending base functionality instead of overriding.
  """

  logging_type: Literal["daily", "per_run"] = "daily"
  logging_base_name: str | None = None
  default_max_width: int | None = None
  timestamp_format: str = "%b, %d %a %I:%M %p"

  @classmethod
  def configure_base_once(cls):
    settings.log_loc_folder.mkdir(exist_ok=True, parents=True)

  @classmethod
  def configure_base_per_runner(cls) -> RootLogger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if __debug__ else logging.INFO)

    paramiko = logging.getLogger("paramiko")
    paramiko.setLevel(logging.WARNING)

    logging.setLogRecordFactory(NamedLogRecord)

    return root

  @classmethod
  def configure_logging_worker(cls, logging_queues: QueueCatchall):
    # Standard library imports
    from concurrent.interpreters import get_current, get_main
    from multiprocessing import current_process

    is_main_process_check = current_process().name == "MainProcess"
    is_main_interpreter_check = get_current() == get_main()

    if is_main_process_check and is_main_interpreter_check:
      raise RuntimeError("configure_logging_worker should only be called from child processes or sub interpreters")

    # Standard library imports
    from logging.handlers import QueueHandler

    root = cls.configure_base_per_runner()

    queue_handler = QueueHandler(logging_queues)
    root.addHandler(queue_handler)

  @classmethod
  def configure_logging_main(  # noqa: C901, PLR0912, PLR0915
    cls,
    rich_console: Console,
    project_name: str,
    asyncio: bool = False,
    log_to_console: bool | Literal["rich"] = "rich",
    queue_console_handler: bool = False,
    logging_queues: Sequence[QueueCatchall] | None = None,
    extra_handlers: Sequence[logging.Handler] | None = None,
  ):

    if logging_queues is None:
      logging_queues = []
    if cls.logging_base_name is None:
      cls.logging_base_name = project_name

    cls.configure_base_once()
    root = cls.configure_base_per_runner()

    log_loc_folder = settings.log_loc_folder
    debug_log_loc = log_loc_folder / f"{cls.logging_base_name}_debug.txt"
    info_log_loc = log_loc_folder / f"{cls.logging_base_name}.txt"

    if cls.logging_type == "per_run":
      # Standard library imports
      from logging.handlers import RotatingFileHandler

      debug_file_handler = RotatingFileHandler(debug_log_loc, maxBytes=0, backupCount=30, delay=True)
      info_file_handler = RotatingFileHandler(info_log_loc, maxBytes=0, backupCount=30, delay=True)
      debug_file_handler.doRollover()
      info_file_handler.doRollover()
    else:
      # First party imports
      from aeth_ext.logging.bases import CustomTimedRotatingFileHandler

      debug_file_handler = CustomTimedRotatingFileHandler(debug_log_loc, when="midnight", backupCount=14, delay=True)
      info_file_handler = CustomTimedRotatingFileHandler(info_log_loc, when="midnight", backupCount=14, delay=True)

    debug_file_handler.setLevel(logging.DEBUG)
    info_file_handler.setLevel(logging.INFO)

    preferred_formatter = get_preferred_logrecord_formatter(
      default_max_width=cls.default_max_width, timestamp_format=cls.timestamp_format
    )

    debug_file_handler.setFormatter(preferred_formatter)
    info_file_handler.setFormatter(preferred_formatter)

    handlers: list[logging.Handler] = [debug_file_handler, info_file_handler]

    if log_to_console:
      if log_to_console == "rich":
        install(show_locals=True)
        console_info_handler = FixedRichHandler(
          # level=logging.DEBUG if __debug__ else logging.INFO,
          show_time=platform == "win32",
          console=rich_console,
          rich_tracebacks=True,
          log_time_format=cls.timestamp_format,
          tracebacks_show_locals=True,
        )
      else:
        console_info_handler = logging.StreamHandler()
        console_info_handler.setLevel(logging.INFO)
        console_formatter = preferred_formatter
        console_info_handler.setFormatter(console_formatter)

      console_info_handler.setLevel(logging.INFO)
      if queue_console_handler:
        handlers.append(console_info_handler)
      else:
        root.addHandler(console_info_handler)

    if asyncio:
      file_log_queue: Queue[logging.LogRecord] = Queue(-1)

      log_receiver = QueueHandler(file_log_queue)

      if extra_handlers:
        handlers.extend(extra_handlers)

      listeners = [
        QueueListener(
          file_log_queue,
          *handlers,
          respect_handler_level=True,
        )
      ]

      if logging_queues:
        for queue in logging_queues:
          new_listener = QueueListener(queue, log_receiver)
          listeners.append(new_listener)

      root.addHandler(log_receiver)

      for listener in listeners:
        listener.start()

        register(listener.stop)
    else:
      for handler in chain(handlers, extra_handlers or []):
        root.addHandler(handler)

    # instead try to find configure_logging_extra via looking for deepest subclass
    sub = cls.get_deepest_subclass()

    packed_kwargs = {
      "rich_console": rich_console,
      "project_name": project_name,
      "log_to_console": log_to_console,
      "queue_console_handler": queue_console_handler,
      "logging_queues": logging_queues,
    }
    sig = signature(sub.configure_logging_extra, annotation_format=Format.FORWARDREF)
    filtered_kwargs = {k: v for k, v in packed_kwargs.items() if k in sig.parameters.keys()}
    sub.configure_logging_extra(**filtered_kwargs)

  @classmethod
  def configure_logging_extra(cls, *args: Any, **kwargs: Any):
    """
    This method is intended to be overridden in a subclass or in a separate module named `logging_config.py`.
    It allows for additional logging configuration beyond the base setup.
    """
    pass

  @classmethod
  def _configure_logserver(cls, queue: AioQueue[WriterItem]):
    """Special method reserved explicitly for the shared_log_processor server's own log handling."""
    # First party imports
    from aeth_ext.shared_log_processor.protocol import TaggedLogRecord
    from aeth_ext.shared_log_processor.server.dispatch import DISPATCH_LOGGER, QueueForwardHandler, ServerFilter

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if __debug__ else logging.INFO)

    paramiko = logging.getLogger("paramiko")
    paramiko.setLevel(logging.WARNING)

    logging.setLogRecordFactory(TaggedLogRecord)

    log_loc_folder = settings.log_loc_folder
    log_loc_folder.mkdir(exist_ok=True, parents=True)

    base_name = cls.logging_base_name or "log_server"
    debug_log_loc = log_loc_folder / f"{base_name}_debug.txt"
    info_log_loc = log_loc_folder / f"{base_name}.txt"

    if cls.logging_type == "per_run":
      # Standard library imports
      from logging.handlers import RotatingFileHandler

      debug_file_handler = RotatingFileHandler(debug_log_loc, maxBytes=0, backupCount=30, delay=True)
      info_file_handler = RotatingFileHandler(info_log_loc, maxBytes=0, backupCount=30, delay=True)
      debug_file_handler.doRollover()
      info_file_handler.doRollover()
    else:
      # First party imports
      from aeth_ext.logging.bases import CustomTimedRotatingFileHandler

      debug_file_handler = CustomTimedRotatingFileHandler(debug_log_loc, when="midnight", backupCount=14, delay=True)
      info_file_handler = CustomTimedRotatingFileHandler(info_log_loc, when="midnight", backupCount=14, delay=True)

    debug_file_handler.setLevel(logging.DEBUG)
    info_file_handler.setLevel(logging.INFO)

    preferred_formatter = get_preferred_logrecord_formatter(
      default_max_width=cls.default_max_width, timestamp_format=cls.timestamp_format
    )
    debug_file_handler.setFormatter(preferred_formatter)
    info_file_handler.setFormatter(preferred_formatter)

    server_filter = ServerFilter()
    debug_file_handler.addFilter(server_filter)
    info_file_handler.addFilter(server_filter)

    DISPATCH_LOGGER.addHandler(debug_file_handler)
    DISPATCH_LOGGER.addHandler(info_file_handler)

    # Forward every record the server emits onto the shared writer queue so the
    # single LogWriterThread handles all logging IO. DISPATCH_LOGGER.propagate is
    # False, so dispatched records never re-enter this handler and cannot loop.
    forward_handler = QueueForwardHandler(queue)
    forward_handler.setLevel(logging.DEBUG)
    root.addHandler(forward_handler)

  @classmethod
  def configure_shared_socket_logging_client(
    cls,
    host: str,
    port: int,
    project_name: str,
    rich_console: Console,
    log_to_console: bool | Literal["rich"] = "rich",
    asyncio: bool = False,
    extra_handler_defs: Sequence[HandlerDef] = (),
  ) -> None:
    """This method is intended to be called from a client process that wants to send its logs to a shared log server."""
    # First party imports
    from aeth_ext.shared_log_processor.client import HandshakeSocketHandler, make_formatter_def, make_handler_def
    from aeth_ext.shared_log_processor.protocol import TaggedLogRecord

    # TODO Review

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    paramiko = logging.getLogger("paramiko")
    paramiko.setLevel(logging.WARNING)

    logging.setLogRecordFactory(TaggedLogRecord)

    debug_log_loc = Path(f"{cls.logging_base_name}_debug.txt")
    info_log_loc = Path(f"{cls.logging_base_name}.txt")

    formatter_def = make_formatter_def(
      FixedFormatter,
      fmt=f"{{libpath: <{cls.default_max_width or __DEFAULT_MAX_WIDTH}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}",
      datefmt=cls.timestamp_format or __DEFAULT_TIMESTAMP_FORMAT,
      style="{",
    )

    if cls.logging_type == "per_run":
      # Standard library imports
      from logging.handlers import RotatingFileHandler

      debug_handler_def = make_handler_def(
        RotatingFileHandler, debug_log_loc, maxBytes=0, backupCount=30, delay=True, formatter=formatter_def, project_name=project_name
      )
      info_handler_def = make_handler_def(
        RotatingFileHandler, info_log_loc, maxBytes=0, backupCount=30, delay=True, formatter=formatter_def, project_name=project_name
      )
    else:
      # First party imports
      from aeth_ext.logging.bases import CustomTimedRotatingFileHandler

      debug_handler_def = make_handler_def(
        CustomTimedRotatingFileHandler,
        debug_log_loc,
        when="midnight",
        backupCount=14,
        delay=True,
        formatter=formatter_def,
        project_name=project_name,
      )
      info_handler_def = make_handler_def(
        CustomTimedRotatingFileHandler,
        info_log_loc,
        when="midnight",
        backupCount=14,
        delay=True,
        formatter=formatter_def,
        project_name=project_name,
      )

    socket_handler = HandshakeSocketHandler(
      program_name=project_name,
      handlers=(debug_handler_def, info_handler_def, *extra_handler_defs),
      host=host,
      port=port,
      logging_base_name=cls.logging_base_name,
    )
    root.addHandler(socket_handler)
    register(socket_handler.close)
