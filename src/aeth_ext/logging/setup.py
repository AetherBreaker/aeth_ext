# Standard library imports
import logging
import sys
from annotationlib import Format
from atexit import register as atexit_register
from contextlib import contextmanager
from inspect import signature
from logging.handlers import QueueListener
from sys import platform
from typing import TYPE_CHECKING, Any, ClassVar, Literal

# Third party imports
from rich.traceback import install

# First party imports
from aeth_ext.logging.bases import FixedRichHandler, TaggedLogRecord
from aeth_ext.logging.config import dict_config, runtime_registry as _registry
from aeth_ext.logging.config.loader import (
  DEFAULT_OVERRIDE_FILENAME,
  assemble_default_config,
  find_override_config,
  load_effective_config,
  pre_resolve,
)
from aeth_ext.logging.config.merge import merge_configs
from aeth_ext.settings import BaseSettings
from aeth_ext.static_eval import parse_and_grab_constants
from aeth_ext.types.subclass_capture import CapturesSubclasses

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator, Mapping, Sequence
  from concurrent.interpreters import Queue as InterpreterQueue
  from multiprocessing import Queue as ProcessQueue
  from pathlib import Path
  from queue import Queue as ThreadQueue

  # Third party imports
  from aiologic import Queue as AioQueue
  from rich.console import Console

  # First party imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem


settings = BaseSettings.get_settings()


__all__ = [
  "BaseLoggingConfig",
  "QueueCatchall",
  "ephemeral_log_to_console",
  "make_per_run_file_handler",
]

type RootLogger = logging.Logger
type QueueCatchall = InterpreterQueue | ProcessQueue[TaggedLogRecord] | ThreadQueue[TaggedLogRecord]

# Override file searched for by socket-logging clients, kept distinct from the
# main-mode DEFAULT_OVERRIDE_FILENAME so local and socket runs can carry
# independent customizations (e.g. propagate=False loggers that must not
# apply when records are meant to reach the socket handler).
SOCKET_OVERRIDE_FILENAME = "logging_config_socket.toml"
# Override file merged onto the default *remote* config a socket client ships
# to the central log server in its handshake.
REMOTE_OVERRIDE_FILENAME = "remote_logging_config.toml"

_DEFAULT_MAX_WIDTH = 51
_DEFAULT_TIMESTAMP_FORMAT = "%b, %d %y - %a %I:%M %p"


def _make_log_format(max_width: int | None = None) -> str:
  return f"{{libpath: <{max_width or _DEFAULT_MAX_WIDTH}}} | [{{asctime}}] | {{levelname: >8}} | {{message}}"


def make_per_run_file_handler(filename: Path, backupCount: int = 30) -> logging.Handler:  # noqa: N803
  """
  Config factory for a per-run rotating file handler.

  Creates a `RotatingFileHandler` that never rotates by size and immediately
  rolls over so each program run writes to a fresh file. Referenced by the
  ``file_per_run`` / ``server_hierarchy_per_run`` / ``remote_per_run``
  packaged config fragments.
  """
  # Standard library imports
  from logging.handlers import RotatingFileHandler

  handler = RotatingFileHandler(filename, maxBytes=0, backupCount=backupCount, delay=True)
  handler.doRollover()
  return handler


def _probe_socket_connection(host: str, port: int, project_name: str) -> bool:
  """Attempt a short-lived TCP connection to *host*:*port* and log the outcome.

  Returns ``True`` if the connection succeeded, ``False`` otherwise.  All
  diagnostic output is emitted via the ``aeth_ext.logging.setup`` logger at
  ``DEBUG`` / ``WARNING`` / ``INFO`` level so it is visible on any handler
  that has already been attached to the root logger.
  """
  # Standard library imports
  import socket as _socket

  log = logging.getLogger(__name__)
  log.debug(
    "[socket probe] Starting connection test to %s:%d for project '%s'",
    host,
    port,
    project_name,
  )
  try:
    log.debug("[socket probe]   Resolving host '%s'...", host)
    addr_infos = _socket.getaddrinfo(host, port, type=_socket.SOCK_STREAM)
    log.debug(
      "[socket probe]   Resolved %d address(es): %s",
      len(addr_infos),
      [ai[4] for ai in addr_infos],
    )
    log.debug("[socket probe]   Attempting TCP connection (timeout=5 s)...")
    with _socket.create_connection((host, port), timeout=5.0) as test_sock:
      log.debug(
        "[socket probe]   Connection established: local=%s  remote=%s",
        test_sock.getsockname(),
        test_sock.getpeername(),
      )
  except _socket.gaierror as exc:
    log.warning("[socket probe]   DNS resolution failed for '%s': %s", host, exc)
    log.warning(
      "[socket probe]   FAILED for %s:%d - HandshakeSocketHandler will buffer records "
      "and retry automatically once the server becomes available.",
      host,
      port,
    )
    return False
  except ConnectionRefusedError as exc:
    log.warning(
      "[socket probe]   Connection refused on %s:%d - server may not be running. %s",
      host,
      port,
      exc,
    )
    log.warning(
      "[socket probe]   FAILED for %s:%d - HandshakeSocketHandler will buffer records "
      "and retry automatically once the server becomes available.",
      host,
      port,
    )
    return False
  except TimeoutError as exc:
    log.warning("[socket probe]   Connection timed out for %s:%d. %s", host, port, exc)
    log.warning(
      "[socket probe]   FAILED for %s:%d - HandshakeSocketHandler will buffer records "
      "and retry automatically once the server becomes available.",
      host,
      port,
    )
    return False
  except OSError as exc:
    log.warning("[socket probe]   OS error during connection test to %s:%d: %s", host, port, exc)
    log.warning(
      "[socket probe]   FAILED for %s:%d - HandshakeSocketHandler will buffer records "
      "and retry automatically once the server becomes available.",
      host,
      port,
    )
    return False
  log.info("[socket probe]   PASSED - server is reachable at %s:%d", host, port)
  return True


@contextmanager
def ephemeral_log_to_console(
  rich_console: Console,
  *,
  max_width: int | None = None,
  timestamp_format: str = _DEFAULT_TIMESTAMP_FORMAT,
  level: int = logging.DEBUG,
) -> Generator[logging.Handler | None]:
  """Context manager that temporarily attaches a console handler to the root logger.

  The handler is attached on entry and unconditionally removed on exit,
  making it safe to use for short diagnostic windows (e.g. a connection probe)
  without leaking handlers into the permanent logging configuration.

  Yields the constructed handler so callers can inspect or further configure it.

  Args:
      rich_console: Rich :class:`~rich.console.Console` the handler renders to.
      max_width: Column width hint (currently unused; kept for call-site compatibility).
      timestamp_format: ``datefmt`` string used by the handler.
      level: Minimum level the handler will emit; defaults to
          :data:`logging.DEBUG` so all records are visible.
  """

  root = logging.getLogger()
  old_level = root.level
  root.setLevel(logging.DEBUG)
  old_except_hook = install(show_locals=True)
  handler: logging.Handler = FixedRichHandler(
    show_time=platform == "win32",
    console=rich_console,
    rich_tracebacks=True,
    log_time_format=timestamp_format,
    tracebacks_show_locals=True,
  )
  handler.setLevel(level)
  root.addHandler(handler)
  try:
    yield handler
  finally:
    root.removeHandler(handler)
    sys.excepthook = old_except_hook
    root.setLevel(old_level)


class BaseLoggingConfig(CapturesSubclasses):
  """
  Assembles the packaged default logging-config TOML fragments, applies any
  project override file, and hands the result to `dict_config`.

  To customize logging, subclass this class and either adjust the class
  variables, override :meth:`modify_config` (which receives the fully
  assembled config dict just before it is applied), or ship a
  ``logging_config.toml`` override file (see
  `aeth_ext.logging.config.loader.find_override_config`).
  """

  logging_type: ClassVar[Literal["daily", "per_run"]] = "daily"
  logging_file_name: ClassVar[str | None] = None
  default_max_width: ClassVar[int] = _DEFAULT_MAX_WIDTH
  timestamp_format: ClassVar[str] = _DEFAULT_TIMESTAMP_FORMAT
  # How a discovered project override file combines with the assembled
  # default config: "replace" swaps it wholesale, "merge" merges named
  # entries onto the default (see aeth_ext.logging.config.merge).
  override_mode: ClassVar[Literal["replace", "merge"]] = "replace"

  # ------------------------------------------------------------------ hooks

  @classmethod
  def modify_config(cls, config: dict[str, Any]) -> dict[str, Any]:
    """
    Hook invoked with the fully assembled config dict just before it is
    applied. Override in a subclass to programmatically adjust the config.
    """
    return config

  @classmethod
  def configure_logging_extra(cls, *args: Any, **kwargs: Any):
    """
    This method is intended to be overridden in a subclass or in a separate module named `logging_config.py`.
    It allows for additional logging configuration beyond the base setup.
    """
    pass

  # -------------------------------------------------------------- internals

  @classmethod
  def _register_format_values(cls) -> None:
    """Register the formatter-related runtime values used by every fragment."""
    _registry.register("log_format", _make_log_format(cls.default_max_width))
    _registry.register("timestamp_format", cls.timestamp_format)

  @classmethod
  def _register_log_paths(cls, base_name: str) -> None:
    """Register the computed debug/info log file paths for *base_name*."""
    folder = settings.log_loc_folder
    _registry.register("debug_log_path", folder / f"{base_name}_debug.log")
    _registry.register("info_log_path", folder / f"{base_name}.log")

  @classmethod
  def _apply_config(cls, fragment_names: Sequence[str], override_filename: str = DEFAULT_OVERRIDE_FILENAME) -> None:
    """Assemble *fragment_names*, apply overrides and the `modify_config` hook, then configure.

    *override_filename* selects which project override file is searched for,
    letting different logging modes (main/socket/worker) carry independent
    overrides. The process log folder is passed as ``log_dir`` so override
    files may use ``logdir://`` filenames.
    """
    config = load_effective_config(
      tuple(fragment_names),
      override_mode=cls.override_mode,
      override_filename=override_filename,
    )
    config = cls.modify_config(config)
    dict_config(config, log_dir=settings.log_loc_folder)

  @staticmethod
  def _start_queue_listeners() -> None:
    """Start (and register atexit stops for) every configured queue listener with attached handlers.

    `dict_config` builds `QueueListener` objects for QueueHandler entries but
    never starts them. Listeners with no handlers (e.g. a worker's outbound
    queue handler) are intentionally skipped - starting one would consume
    records some other process is meant to drain.
    """
    for name in logging.getHandlerNames():
      handler = logging.getHandlerByName(name)
      listener: QueueListener | None = getattr(handler, "listener", None)
      if listener is not None and listener.handlers and listener._thread is None:
        listener.start()
        atexit_register(listener.stop)

  @staticmethod
  def _attach_queue_drains(root: RootLogger, logging_queues: Sequence[QueueCatchall]) -> None:
    """Drain each producer queue (workers, sub-interpreters) into the root logger's handlers.

    Without an active drain, an unbounded producer (e.g. a
    :class:`multiprocessing.Queue` fed by many pool workers) will eventually
    fill the underlying OS pipe buffer, block the queue's feeder thread, and
    deadlock every worker on its next ``log.emit()`` call.
    """
    if not logging_queues:
      return
    forwarding_handlers = tuple(root.handlers)
    for q in logging_queues:
      listener = QueueListener(q, *forwarding_handlers, respect_handler_level=True)
      listener.start()
      atexit_register(listener.stop)

  # ----------------------------------------------------------- entry points

  @classmethod
  def configure_logging_main(
    cls,
    rich_console: Console,
    project_name: str,
    asyncio: bool = False,
    log_to_console: bool | Literal["rich"] = "rich",
    queue_console_handler: bool = False,
    logging_queues: Sequence[QueueCatchall] | None = None,
    extra_handlers: Sequence[logging.Handler] | None = None,
  ) -> None:
    """Configure logging for a main process from the packaged default fragments."""
    logging_queues = list(logging_queues or [])
    if cls.logging_file_name is None:
      cls.logging_file_name = project_name

    settings.log_loc_folder.mkdir(exist_ok=True, parents=True)
    logging.setLogRecordFactory(TaggedLogRecord)

    fragments = ["main_base", "file_daily" if cls.logging_type == "daily" else "file_per_run"]
    if log_to_console == "rich":
      install(show_locals=True)
      fragments.append("console_rich")
    elif log_to_console:
      fragments.append("console_plain")
    if asyncio:
      fragments.append("async_queue")

    cls._register_format_values()
    cls._register_log_paths(cls.logging_file_name)
    _registry.register("project_name", project_name)
    _registry.register("root_level", "DEBUG" if __debug__ else "INFO")
    _registry.register("console", rich_console)
    _registry.register("console_show_time", platform == "win32")

    # Handler wiring for the async (queue-wrapped) variant: file handlers are
    # always drained through the catchall queue; the console handler joins them
    # only when queue_console_handler is set, otherwise it stays on the root.
    queued_names = ["debug_file", "info_file"]
    root_names = ["queue_catchall"]
    if log_to_console:
      (queued_names if queue_console_handler else root_names).append("console")
    _registry.register("queued_handler_names", queued_names)
    _registry.register("root_handler_names", root_names)

    cls._apply_config(fragments)

    root = logging.getLogger()
    for handler in extra_handlers or []:
      root.addHandler(handler)

    cls._start_queue_listeners()
    cls._attach_queue_drains(root, logging_queues)

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
  def configure_logging_worker(cls, logging_queues: QueueCatchall) -> None:
    """Configure logging for a worker process/sub-interpreter: everything forwards to *logging_queues*."""
    # Standard library imports
    from concurrent.interpreters import get_current, get_main
    from multiprocessing import current_process

    is_main_process_check = current_process().name == "MainProcess"
    is_main_interpreter_check = get_current() == get_main()

    if is_main_process_check and is_main_interpreter_check:
      raise RuntimeError("configure_logging_worker should only be called from child processes or sub interpreters")

    logging.setLogRecordFactory(TaggedLogRecord)

    _registry.register("worker_queue", logging_queues)
    _registry.register("root_level", "DEBUG" if __debug__ else "INFO")

    cls._apply_config(["worker"])

  @classmethod
  def _configure_logserver(cls, queue: AioQueue[WriterItem]) -> dict[str, Any]:
    """Special method reserved explicitly for the central_log_server server's own log handling.

    Applies the global ``log_server_root`` fragment (whose root just forwards
    every record onto *queue*) and returns the assembled *server hierarchy*
    config - the server's own file output, which the writer thread applies
    into a private hierarchy as its "server pseudo-client". The returned
    config's ``runtime://`` values resolve in-process against the
    registrations made here when that hierarchy is built.
    """
    logging.setLogRecordFactory(TaggedLogRecord)
    settings.log_loc_folder.mkdir(exist_ok=True, parents=True)

    # Third party imports
    from rich import get_console

    _constants = parse_and_grab_constants(
      expected_constants={
        "PROJECT_NAME": "project_name",
        "TESTING": "testing",
      }
    )
    project_name = _constants.get("project_name") or cls.logging_file_name or "log_server"
    testing = _constants.get("testing", False)

    frags = ["server_hierarchy_daily"]

    cls._register_format_values()
    cls._register_log_paths(cls.logging_file_name or project_name)
    _registry.register("root_level", "DEBUG")
    _registry.register("writer_queue", queue)
    if testing:
      _registry.register("project_name", project_name)
      _registry.register("console", get_console())
      _registry.register("console_show_time", platform == "win32")
      frags.append("console_rich")

    cls._apply_config(["log_server_root"])

    return assemble_default_config(*frags)

  @classmethod
  def get_default_remote_config(cls, logging_file_name: str) -> dict[str, Any]:
    """Assemble the default remote config a socket client ships to the central log server.

    A project may customize the result purely in TOML by shipping a
    ``remote_logging_config.toml`` override file (discovered via
    `find_override_config`); it is merged onto the packaged default using
    named-entry semantics before resolution.

    The result is client-side resolved via `pre_resolve`: formatter values are
    materialised locally, while the registered ``remote_*_filename`` values
    resolve to ``logdir://`` strings that the *server* roots beneath its
    per-program log directory.
    """
    cls._register_format_values()
    _registry.register("remote_debug_filename", f"logdir://{logging_file_name}_debug.log")
    _registry.register("remote_info_filename", f"logdir://{logging_file_name}.log")
    config = assemble_default_config("remote_daily" if cls.logging_type == "daily" else "remote_per_run")
    override_path = find_override_config(REMOTE_OVERRIDE_FILENAME)
    if override_path is not None:
      # Standard library imports
      import tomllib

      config = merge_configs(config, tomllib.loads(override_path.read_text(encoding="utf-8")))
    return pre_resolve(config)

  @classmethod
  def configure_shared_socket_logging_client(
    cls,
    project_name: str,
    rich_console: Console,
    host: str | None = None,
    port: int | None = None,
    remote_config: Mapping[str, Any] | None = None,
    testing: bool = False,
  ) -> None:
    """This method is intended to be called from a client process that wants to send its logs to a shared log server."""
    if host is None:
      host = settings.log_conn_host
    if port is None:
      port = settings.log_conn_port

    if cls.logging_file_name is None:
      cls.logging_file_name = project_name

    if remote_config is None:
      remote_config = cls.get_default_remote_config(cls.logging_file_name)

    logging.setLogRecordFactory(TaggedLogRecord)

    with ephemeral_log_to_console(
      rich_console,
      max_width=cls.default_max_width,
      timestamp_format=cls.timestamp_format,
    ):
      _connection_ok = _probe_socket_connection(host, port, project_name)
      if not _connection_ok:
        raise RuntimeError(
          f"Failed to connect to log server at {host}:{port} for project '{project_name}'. "
          "Check the server is running and reachable, and that the host/port are correct."
        )

    cls._register_format_values()
    _registry.register("project_name", project_name)
    _registry.register("root_level", "DEBUG")
    _registry.register("log_host", host)
    _registry.register("log_port", port)
    _registry.register("remote_config", dict(remote_config))

    if testing:
      # Local file/console logging alongside the socket handler, assembled as
      # one combined config so a single dict_config application wires it all.
      settings.log_loc_folder.mkdir(exist_ok=True, parents=True)
      install(show_locals=True)
      cls._register_log_paths(cls.logging_file_name)
      _registry.register("console", rich_console)
      _registry.register("console_show_time", platform == "win32")
      _registry.register("queued_handler_names", ["debug_file", "info_file"])
      _registry.register("root_handler_names", ["queue_catchall", "console", "socket"])

      fragments = [
        "main_base",
        "file_daily" if cls.logging_type == "daily" else "file_per_run",
        "console_rich",
        "socket_client",
        "async_queue",
      ]
    else:
      fragments = ["socket_client"]

    cls._apply_config(fragments, override_filename=SOCKET_OVERRIDE_FILENAME)
    cls._start_queue_listeners()

    # The probe above only proved the server is reachable; this proves it
    # actually *accepted* our remote config, failing fast on rejection.
    # First party imports
    from aeth_ext.central_log_server.client import HandshakeSocketHandler

    socket_handler = logging.getHandlerByName("socket")
    if isinstance(socket_handler, HandshakeSocketHandler):
      socket_handler.connect_and_verify()
