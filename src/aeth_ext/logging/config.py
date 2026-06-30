# Standard library imports
import logging
from annotationlib import Format
from atexit import register
from concurrent.interpreters import get_current, get_main
from inspect import signature
from logging.handlers import QueueHandler, QueueListener
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
  from rich.console import Console


if get_current() == get_main():
  settings = BaseSettings.get_settings()


__all__ = [
  "BaseLoggingConfig",
  "QueueCatchall",
  "get_global_log_receiver",
  "get_preferred_logrecord_formatter",
  "set_preferred_logrecord_formatter",
]

type RootLogger = logging.Logger
type QueueCatchall = InterpreterQueue | ProcessQueue[NamedLogRecord] | ThreadQueue[NamedLogRecord]

__global_log_receiver: QueueHandler | None = None
__preferred_file_formatter: FixedFormatter | None = None
__DEFAULT_MAX_WIDTH = 36
__DEFAULT_TIMESTAMP_FORMAT = "%b, %d %a %I:%M %p"


def get_global_log_receiver() -> QueueHandler:
  if __global_log_receiver is None:
    raise RuntimeError("Global log receiver has not been configured yet")
  return __global_log_receiver


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

  @classmethod
  def configure_base_once(cls):
    settings.log_loc_folder.mkdir(exist_ok=True, parents=True)

  @classmethod
  def configure_base_per_runner(
    cls,
    project_name: str,
  ) -> RootLogger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if __debug__ else logging.INFO)

    paramiko = logging.getLogger("paramiko")
    paramiko.setLevel(logging.WARNING)

    NamedLogRecord.PROJECT_NAME = project_name

    logging.setLogRecordFactory(NamedLogRecord)

    return root

  @classmethod
  def configure_logging_worker(
    cls,
    logging_queues: QueueCatchall,
    project_name: str,
    logging_base_name: str | None = None,
  ):
    # Standard library imports
    from concurrent.interpreters import get_current, get_main
    from multiprocessing import current_process

    is_main_process_check = current_process().name == "MainProcess"
    is_main_interpreter_check = get_current() == get_main()

    if is_main_process_check and is_main_interpreter_check:
      raise RuntimeError("configure_logging_worker should only be called from child processes or sub interpreters")

    # Standard library imports
    from logging.handlers import QueueHandler

    if logging_base_name is None:
      logging_base_name = project_name

    root = cls.configure_base_per_runner(project_name=project_name)

    queue_handler = QueueHandler(logging_queues)
    root.addHandler(queue_handler)

  @classmethod
  def configure_logging_main(  # noqa: PLR0915
    cls,
    rich_console: Console,
    project_name: str,
    logging_type: Literal["daily", "per_run"] = "daily",
    logging_base_name: str | None = None,
    default_max_width: int | None = None,
    timestamp_format: str = "%b, %d %a %I:%M %p",
    log_to_console: bool | Literal["rich"] = "rich",
    queue_console_handler: bool = False,
    logging_queues: Sequence[QueueCatchall] | None = None,
  ):

    if logging_queues is None:
      logging_queues = []
    if logging_base_name is None:
      logging_base_name = project_name

    cls.configure_base_once()
    root = cls.configure_base_per_runner(project_name=project_name)

    log_loc_folder = settings.log_loc_folder
    debug_log_loc = log_loc_folder / f"{logging_base_name}_debug.txt"
    info_log_loc = log_loc_folder / f"{logging_base_name}.txt"

    if logging_type == "per_run":
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

    preferred_formatter = get_preferred_logrecord_formatter(default_max_width=default_max_width, timestamp_format=timestamp_format)

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
          log_time_format=timestamp_format,
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

    file_log_queue: Queue[logging.LogRecord] = Queue(-1)

    global __global_log_receiver
    __global_log_receiver = QueueHandler(file_log_queue)

    listeners = [
      QueueListener(
        file_log_queue,
        *handlers,
        respect_handler_level=True,
      )
    ]

    if logging_queues:
      for queue in logging_queues:
        new_listener = QueueListener(queue, __global_log_receiver)
        listeners.append(new_listener)

    root.addHandler(__global_log_receiver)

    for listener in listeners:
      listener.start()

      register(listener.stop)

    # instead try to find configure_logging_extra via looking for deepest subclass
    sub = cls.get_deepest_subclass()

    packed_kwargs = {
      "rich_console": rich_console,
      "project_name": project_name,
      "logging_type": logging_type,
      "logging_base_name": logging_base_name,
      "default_max_width": default_max_width,
      "timestamp_format": timestamp_format,
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
