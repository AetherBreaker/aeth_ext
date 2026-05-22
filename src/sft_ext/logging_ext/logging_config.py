from __future__ import annotations

import logging
from importlib import import_module
from queue import Queue
from typing import TYPE_CHECKING, Literal

from sft_ext.logging_ext.logging_bases import FixedFormatter, FixedLogRecord, FixedRichHandler

if TYPE_CHECKING:
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


ROOT = logging.getLogger()
ROOT.setLevel(logging.DEBUG if __debug__ else logging.INFO)


paramiko = logging.getLogger("paramiko")
paramiko.setLevel(logging.WARNING)


def configure_logging(
  rich_console: Console,
  project_name: str,
  logging_type: Literal["daily", "per_run"] = "daily",
  logging_base_name: str | None = None,
  default_max_width: int = 36,
  timestamp_format: str = "%b, %d %a %I:%M %p",
) -> Queue[logging.LogRecord]:
  from multiprocessing import parent_process

  if parent_process() is not None:
    raise RuntimeError("configure_logging should only be called from the main process")

  import atexit
  from logging.handlers import QueueHandler, QueueListener
  from sys import platform

  from rich.traceback import install

  if logging_base_name is None:
    logging_base_name = project_name

  LOG_LOC_FOLDER = SETTINGS.persisted_dir_loc / "logs"
  DEBUG_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}_debug.txt"
  INFO_LOG_LOC = LOG_LOC_FOLDER / f"{logging_base_name}.txt"

  FixedLogRecord.DEFAULT_MAX_WIDTH = default_max_width
  FixedLogRecord.PROJECT_NAME = project_name

  logging.setLogRecordFactory(FixedLogRecord)

  global RICH_CONSOLE
  RICH_CONSOLE = rich_console

  LOG_LOC_FOLDER.mkdir(exist_ok=True, parents=True)

  install(show_locals=True)

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

  file_formatter = FixedFormatter(
    fmt=f"{{libpath: <{default_max_width}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}",
    datefmt=timestamp_format,
    style="{",
  )

  debug_file_handler.setFormatter(file_formatter)
  info_file_handler.setFormatter(file_formatter)

  console_info_handler = FixedRichHandler(
    # level=logging.DEBUG if __debug__ else logging.INFO,
    show_time=platform == "win32",
    console=rich_console,
    rich_tracebacks=True,
    log_time_format=timestamp_format,
    tracebacks_show_locals=True,
  )

  console_info_handler.setLevel(logging.INFO)

  file_log_queue: Queue[logging.LogRecord] = Queue(-1)

  queue_handler = QueueHandler(file_log_queue)

  ROOT.addHandler(console_info_handler)

  file_queue_listener = QueueListener(
    file_log_queue,
    debug_file_handler,
    info_file_handler,
    respect_handler_level=True,
  )

  ROOT.addHandler(queue_handler)

  file_queue_listener.start()

  atexit.register(file_queue_listener.stop)

  return file_log_queue
