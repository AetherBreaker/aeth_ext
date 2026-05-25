from __future__ import annotations

import logging
from atexit import register
from collections.abc import Sequence
from importlib import import_module
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
from sys import platform
from typing import TYPE_CHECKING, Literal

from rich.traceback import install

from sft_ext.logging_ext.logging_bases import FixedFormatter, FixedLogRecord, FixedRichHandler

if TYPE_CHECKING:
  from concurrent.interpreters import Queue as InterpreterQueue
  from multiprocessing import Queue as ProcessQueue
  from queue import Queue as ThreadQueue

  from rich.console import Console

  from sft_ext.settings import BaseSettings


try:
  settings_module = import_module("environment_init_vars")
  SETTINGS: BaseSettings = settings_module.SETTINGS
except ImportError:
  try:
    settings_module = import_module("environment_settings")
    SETTINGS: BaseSettings = settings_module.SETTINGS(**{})
  except (ImportError, AttributeError):
    from sft_ext.settings import BaseSettings

    SETTINGS: BaseSettings = BaseSettings(**{})


# ROOT = logging.getLogger()
# ROOT.setLevel(logging.DEBUG if __debug__ else logging.INFO)


# paramiko = logging.getLogger("paramiko")
# paramiko.setLevel(logging.WARNING)


# def configure_logging(
#   rich_console: Console,
#   project_name: str,
#   logging_type: Literal["daily", "per_run"] = "daily",
#   logging_base_name: str | None = None,
#   default_max_width: int = 36,
#   timestamp_format: str = "%b, %d %a %I:%M %p",
# ) -> Queue[logging.LogRecord]:
#   from multiprocessing import parent_process

#   if parent_process() is not None:
#     raise RuntimeError("configure_logging should only be called from the main process")

#   import atexit
#   from logging.handlers import QueueHandler, QueueListener
#   from sys import platform

#   from rich.traceback import install

#   if logging_base_name is None:
#     logging_base_name = project_name

#   LOG_LOC_FOLDER = SETTINGS.log_loc_folder
#   DEBUG_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}_debug.txt"
#   INFO_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}.txt"

#   FixedLogRecord.DEFAULT_MAX_WIDTH = default_max_width
#   FixedLogRecord.PROJECT_NAME = project_name

#   logging.setLogRecordFactory(FixedLogRecord)

#   LOG_LOC_FOLDER.mkdir(exist_ok=True, parents=True)

#   install(show_locals=True)

#   if logging_type == "per_run":
#     from logging.handlers import RotatingFileHandler

#     debug_file_handler = RotatingFileHandler(DEBUG_LOG_LOC, maxBytes=0, backupCount=30, delay=True)
#     info_file_handler = RotatingFileHandler(INFO_LOG_LOC, maxBytes=0, backupCount=30, delay=True)
#     debug_file_handler.doRollover()
#     info_file_handler.doRollover()
#   else:
#     from sft_ext.logging_ext.logging_bases import CustomTimedRotatingFileHandler

#     debug_file_handler = CustomTimedRotatingFileHandler(DEBUG_LOG_LOC, when="midnight", backupCount=14, delay=True)
#     info_file_handler = CustomTimedRotatingFileHandler(INFO_LOG_LOC, when="midnight", backupCount=14, delay=True)

#   debug_file_handler.setLevel(logging.DEBUG)
#   info_file_handler.setLevel(logging.INFO)

#   file_formatter = FixedFormatter(
#     fmt=f"{{libpath: <{default_max_width}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}",
#     datefmt=timestamp_format,
#     style="{",
#   )

#   debug_file_handler.setFormatter(file_formatter)
#   info_file_handler.setFormatter(file_formatter)

#   console_info_handler = FixedRichHandler(
#     # level=logging.DEBUG if __debug__ else logging.INFO,
#     show_time=platform == "win32",
#     console=rich_console,
#     rich_tracebacks=True,
#     log_time_format=timestamp_format,
#     tracebacks_show_locals=True,
#   )

#   console_info_handler.setLevel(logging.INFO)

#   file_log_queue: Queue[logging.LogRecord] = Queue(-1)

#   queue_handler = QueueHandler(file_log_queue)

#   ROOT.addHandler(console_info_handler)

#   file_queue_listener = QueueListener(
#     file_log_queue,
#     debug_file_handler,
#     info_file_handler,
#     respect_handler_level=True,
#   )

#   ROOT.addHandler(queue_handler)

#   file_queue_listener.start()

#   atexit.register(file_queue_listener.stop)

#   return file_log_queue

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
  logging_base_name: str,
  default_max_width: int = 36,
) -> RootLogger:
  ROOT = logging.getLogger()
  ROOT.setLevel(logging.DEBUG if __debug__ else logging.INFO)

  paramiko = logging.getLogger("paramiko")
  paramiko.setLevel(logging.WARNING)

  FixedLogRecord.DEFAULT_MAX_WIDTH = default_max_width
  FixedLogRecord.PROJECT_NAME = project_name

  logging.setLogRecordFactory(FixedLogRecord)

  return ROOT


def configure_logging_worker(
  logging_queue: QueueCatchall,
  project_name: str,
  logging_base_name: str | None = None,
  default_max_width: int = 36,
):
  from multiprocessing import current_process

  if current_process().name == "MainProcess":
    raise RuntimeError("configure_logging_worker should only be called from child processes")

  from concurrent.interpreters import get_current, get_main

  if get_current() == get_main():
    raise RuntimeError("configure_logging_worker should only be called from non-main interpreters")

  from logging.handlers import QueueHandler

  if logging_base_name is None:
    logging_base_name = project_name

  root = configure_base_per_runner(
    project_name=project_name,
    logging_base_name=logging_base_name,
    default_max_width=default_max_width,
  )

  queue_handler = QueueHandler(logging_queue)
  root.addHandler(queue_handler)


def configure_logging_main(
  rich_console: Console,
  project_name: str,
  logging_type: Literal["daily", "per_run"] = "daily",
  logging_base_name: str | None = None,
  default_max_width: int = 36,
  timestamp_format: str = "%b, %d %a %I:%M %p",
  log_to_console: bool | Literal["rich"] = "rich",
  queue_console_handler: bool = False,
  logging_queues: Sequence[QueueCatchall] = None,
):

  if logging_queues is None:
    logging_queues = []
  if logging_base_name is None:
    logging_base_name = project_name

  configure_base_once()
  root = configure_base_per_runner(
    project_name=project_name,
    logging_base_name=logging_base_name,
    default_max_width=default_max_width,
  )

  LOG_LOC_FOLDER = SETTINGS.log_loc_folder
  DEBUG_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}_debug.txt"
  INFO_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}.txt"

  if logging_type == "per_run":
    from logging.handlers import RotatingFileHandler

    debug_file_handler = RotatingFileHandler(DEBUG_LOG_LOC, maxBytes=0, backupCount=30, delay=True)
    info_file_handler = RotatingFileHandler(INFO_LOG_LOC, maxBytes=0, backupCount=30, delay=True)
    debug_file_handler.doRollover()
    info_file_handler.doRollover()
  else:
    from sft_ext.logging_ext.logging_bases import CustomTimedRotatingFileHandler

    debug_file_handler = CustomTimedRotatingFileHandler(DEBUG_LOG_LOC, when="midnight", backupCount=14, delay=True)
    info_file_handler = CustomTimedRotatingFileHandler(INFO_LOG_LOC, when="midnight", backupCount=14, delay=True)

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
