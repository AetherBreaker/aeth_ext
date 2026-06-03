import logging
from atexit import register
from concurrent.interpreters import get_current, get_main
from importlib import import_module
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
from sys import platform
from typing import TYPE_CHECKING, Literal

from rich.traceback import install

from sft_ext.logging.logging_bases import FixedFormatter, FixedLogRecord, FixedRichHandler

if TYPE_CHECKING:
  from collections.abc import Sequence
  from concurrent.interpreters import Queue as InterpreterQueue
  from multiprocessing import Queue as ProcessQueue
  from queue import Queue as ThreadQueue

  from rich.console import Console

  from sft_ext.settings import BaseSettings


if get_current() == get_main():
  try:
    settings_module = import_module("environment_init_vars")
    SETTINGS: BaseSettings = settings_module.SETTINGS
  except ImportError:
    try:
      settings_module = import_module("environment_settings")
      SETTINGS: BaseSettings = settings_module.SETTINGS(**{})
    except ImportError, AttributeError:
      from sft_ext.settings import BaseSettings

      SETTINGS: BaseSettings = BaseSettings(**{})


type RootLogger = logging.Logger
type QueueCatchall = InterpreterQueue | ProcessQueue[FixedLogRecord] | ThreadQueue[FixedLogRecord]

GLOBAL_LOG_RECEIVER: QueueHandler | None = None


def get_global_log_receiver() -> QueueHandler:
  if GLOBAL_LOG_RECEIVER is None:
    raise RuntimeError("Global log receiver has not been configured yet")
  return GLOBAL_LOG_RECEIVER


def configure_base_once():
  SETTINGS.log_loc_folder.mkdir(exist_ok=True, parents=True)


def configure_base_per_runner(
  project_name: str,
) -> RootLogger:
  root = logging.getLogger()
  root.setLevel(logging.DEBUG if __debug__ else logging.INFO)

  paramiko = logging.getLogger("paramiko")
  paramiko.setLevel(logging.WARNING)

  FixedLogRecord.PROJECT_NAME = project_name

  logging.setLogRecordFactory(FixedLogRecord)

  return root


def configure_logging_worker(
  logging_queues: QueueCatchall,
  project_name: str,
  logging_base_name: str | None = None,
):
  from concurrent.interpreters import get_current, get_main
  from multiprocessing import current_process

  is_main_process_check = current_process().name == "MainProcess"
  is_main_interpreter_check = get_current() == get_main()

  if is_main_process_check and is_main_interpreter_check:
    raise RuntimeError("configure_logging_worker should only be called from child processes or sub interpreters")

  from logging.handlers import QueueHandler

  if logging_base_name is None:
    logging_base_name = project_name

  root = configure_base_per_runner(project_name=project_name)

  queue_handler = QueueHandler(logging_queues)
  root.addHandler(queue_handler)


def configure_logging_main(  # noqa: C901, PLR0912, PLR0915
  rich_console: Console,
  project_name: str,
  logging_type: Literal["daily", "per_run"] = "daily",
  logging_base_name: str | None = None,
  default_max_width: int = 36,
  timestamp_format: str = "%b, %d %a %I:%M %p",
  log_to_console: bool | Literal["rich"] = "rich",
  queue_console_handler: bool = False,
  logging_queues: Sequence[QueueCatchall] | None = None,
):

  if logging_queues is None:
    logging_queues = []
  if logging_base_name is None:
    logging_base_name = project_name

  configure_base_once()
  root = configure_base_per_runner(project_name=project_name)

  log_loc_folder = SETTINGS.log_loc_folder
  debug_log_loc = log_loc_folder / f"{logging_base_name}_debug.txt"
  info_log_loc = log_loc_folder / f"{logging_base_name}.txt"

  if logging_type == "per_run":
    from logging.handlers import RotatingFileHandler

    debug_file_handler = RotatingFileHandler(debug_log_loc, maxBytes=0, backupCount=30, delay=True)
    info_file_handler = RotatingFileHandler(info_log_loc, maxBytes=0, backupCount=30, delay=True)
    debug_file_handler.doRollover()
    info_file_handler.doRollover()
  else:
    from sft_ext.logging.logging_bases import CustomTimedRotatingFileHandler

    debug_file_handler = CustomTimedRotatingFileHandler(debug_log_loc, when="midnight", backupCount=14, delay=True)
    info_file_handler = CustomTimedRotatingFileHandler(info_log_loc, when="midnight", backupCount=14, delay=True)

  debug_file_handler.setLevel(logging.DEBUG)
  info_file_handler.setLevel(logging.INFO)

  preferred_formatter = FixedFormatter(
    fmt=f"{{libpath: <{default_max_width}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}",
    datefmt=timestamp_format,
    style="{",
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
        log_time_format=timestamp_format,
        tracebacks_show_locals=True,
      )
    else:
      console_info_handler = logging.StreamHandler()
      console_info_handler.setLevel(logging.INFO)
      console_formatter = logging.Formatter(fmt="[{asctime}] | {levelname: >8} | {message}", datefmt=timestamp_format, style="{")
      console_info_handler.setFormatter(console_formatter)

    console_info_handler.setLevel(logging.INFO)
    if queue_console_handler:
      handlers.append(console_info_handler)
    else:
      root.addHandler(console_info_handler)

  file_log_queue: Queue[logging.LogRecord] = Queue(-1)

  global GLOBAL_LOG_RECEIVER
  GLOBAL_LOG_RECEIVER = QueueHandler(file_log_queue)

  listeners = [
    QueueListener(
      file_log_queue,
      *handlers,
      respect_handler_level=True,
    )
  ]

  if logging_queues:
    for queue in logging_queues:
      new_listener = QueueListener(queue, GLOBAL_LOG_RECEIVER)
      listeners.append(new_listener)

  root.addHandler(GLOBAL_LOG_RECEIVER)

  for listener in listeners:
    listener.start()

    register(listener.stop)
