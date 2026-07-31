"""Utilities for querying a package's logging configuration in an isolated subprocess.

Importing a child package to call its config function would register loggers into
the current process's global hierarchy, which is unacceptable when the caller is the
parent that needs a clean separation between its own logging state and the child's.
:func:`query_logging_configs` avoids this entirely by spawning a fresh Python
interpreter (``spawn`` start method), running the target function there, and returning
the result over a :class:`multiprocessing.connection.Connection` pipe.

Defining a config provider
---------------------------
Any package that can be launched as a subprocess should expose a zero-argument
callable that returns a :class:`LoggingConfigResult`-compatible dict, e.g.::

    # my_package/logging_configs.py
    from aeth_ext.central_log_server.client.config_provider import LoggingConfigResult


    def get_logging_configs() -> LoggingConfigResult:
      return {
        "local": {...},  # dict-based file-handler config for local logging
        "remote": {...},  # dict-based config sent to the central log server
        "program_name": "my-package",  # name used in the server handshake
        "testing": False,  # True if the program logs both locally AND to the server
      }

Querying from the parent
------------------------
The parent passes the dotted import path (and optionally the function name after
``:``) to :func:`query_logging_configs`::

    from aeth_ext.central_log_server.client.config_provider import query_logging_configs

    configs = query_logging_configs("my_package.logging_configs:get_logging_configs")
    # or, using the default function name:
    configs = query_logging_configs("my_package.logging_configs")
    local_cfg = configs["local"]
    remote_cfg = configs["remote"]
"""

# Standard library imports
import multiprocessing
import multiprocessing.connection
from typing import Any, Literal, TypedDict

__all__ = ["LoggingConfigResult", "query_logging_configs"]


class LoggingConfigResult(TypedDict):
  """The logging configurations and metadata a package exposes for subprocess use.

  All four fields are required.  Custom ``get_logging_configs`` callables must
  supply all of them; the automatic fallback via
  :class:`~aeth_ext.logging.setup.BaseLoggingConfig` populates them from the
  package's ALL-CAPS constants.
  """

  local: dict[str, Any]
  """File-handler-based local logging config (compatible with :func:`logging.config.dictConfig`).

  Used to build a private logging hierarchy inside the drainer when
  ``testing`` is ``True`` or ``log_locally`` is set on the drainer.
  """

  remote: dict[str, Any]
  """Config shipped to the central log server in the :class:`~aeth_ext.central_log_server.protocol.ClientHandshake`.

  Empty when the package uses self-managed (``mode="main"``) logging.
  """

  program_name: str
  """Identifier sent to the server in the handshake and used in log messages."""

  testing: bool
  """Whether the package intends to log both locally *and* to the server.

  When ``True`` the drainer automatically activates its local hierarchy even if
  ``log_locally`` was not explicitly set.
  """


def query_logging_configs(
  target: str,
  *,
  mode: Literal["socket", "main"] = "socket",
  timeout: float = 30.0,
) -> LoggingConfigResult:
  """Return a package's logging configs by running the query in an isolated subprocess.

  *target* is an import path of the form ``"module.path:function_name"``.  The
  named function must accept no arguments and return a
  :class:`LoggingConfigResult`-compatible dict.  When the function name is
  omitted (e.g. ``"my_package"``), ``get_logging_configs`` is tried first; if
  the function is absent from the module, the subprocess falls back to
  :meth:`~aeth_ext.logging.setup.BaseLoggingConfig.get_default_socket_logging_configs`
  (when *mode* is ``"socket"``) or
  :meth:`~aeth_ext.logging.setup.BaseLoggingConfig.get_default_main_logging_configs`
  (when *mode* is ``"main"``), which infer the config from ALL-CAPS constants
  defined in the target package's ``__main__.py`` / ``__init__.py``.

  When an explicit function name is provided via ``:``, the fallback is **not**
  used — a missing function raises :exc:`RuntimeError`.

  The subprocess is started with the ``spawn`` start method so the parent's
  global logging registry is never touched by the child package's imports.

  Args:
      target: Dotted import path, optionally suffixed with ``:function_name``.
      mode: Controls which :class:`~aeth_ext.logging.setup.BaseLoggingConfig`
          fallback is used if ``get_logging_configs`` is absent.
          ``"socket"`` → socket-client configs; ``"main"`` → self-managed
          file-logging configs with an empty remote dict.
      timeout: Maximum seconds to wait for the subprocess to produce a result
          before raising :exc:`TimeoutError` and terminating it.

  Raises:
      RuntimeError: If the subprocess exits non-zero or the target callable
          raises an exception.
      TimeoutError: If the subprocess does not complete within *timeout* seconds.
  """
  ctx = multiprocessing.get_context("spawn")
  parent_conn, child_conn = ctx.Pipe(duplex=False)
  proc = ctx.Process(target=_query_worker, args=(target, mode, child_conn), daemon=True)
  proc.start()
  child_conn.close()  # parent holds the read end only

  try:
    proc.join(timeout=timeout)
  finally:
    if proc.is_alive():
      proc.terminate()
      proc.join(timeout=5.0)

  if proc.is_alive() or not parent_conn.poll():
    parent_conn.close()
    raise TimeoutError(f"query_logging_configs timed out after {timeout}s for target {target!r}")

  try:
    result = parent_conn.recv()
  finally:
    parent_conn.close()

  if isinstance(result, BaseException):
    raise RuntimeError(f"query_logging_configs failed for target {target!r}") from result  # noqa: TRY004

  return result


def _find_package_file(module_path: str) -> str | None:
  """Return the ``__file__`` path for *module_path* without importing it.

  Tries ``importlib.util.find_spec`` and, for packages without an
  ``__init__.py``, searches ``__main__.py`` inside each search location.
  Returns ``None`` when the package cannot be located.
  """
  # Standard library imports
  import importlib.util
  from pathlib import Path

  try:
    spec = importlib.util.find_spec(module_path)
  except ModuleNotFoundError, ValueError:
    return None
  if spec is None:
    return None
  if spec.origin and spec.origin != "namespace":
    return spec.origin
  if spec.submodule_search_locations:
    for loc in spec.submodule_search_locations:
      for candidate_name in ("__main__.py", "__init__.py"):
        candidate = Path(loc) / candidate_name
        if candidate.is_file():
          return str(candidate)
  return None


def _resolve_target(target: str, mode: str) -> object:
  """Import the target and return a result dict; used by :func:`_query_worker`.

  Returns the ``LoggingConfigResult``-compatible dict. Falls back to
  ``BaseLoggingConfig`` when the default ``get_logging_configs`` name is used
  and the module does not provide it.
  """
  # Standard library imports
  import importlib

  module_path, sep, func_name = target.rpartition(":")
  using_default_func = not sep
  if not sep:
    module_path = target
    func_name = "get_logging_configs"

  mod = None
  try:
    mod = importlib.import_module(module_path)
  except ImportError:
    pass

  func = getattr(mod, func_name, None) if mod is not None else None

  if func is not None:
    return func()
  if not using_default_func:
    raise AttributeError(f"Module {module_path!r} has no attribute {func_name!r}")

  # Default func not found — fall back to BaseLoggingConfig.
  # Use importlib to avoid a static import cycle:
  #   client/__init__ → config_provider → setup → client/__init__
  # Standard library imports
  import importlib

  # First party imports
  from aeth_ext.static_eval import parse_and_grab_constants

  _setup = importlib.import_module("aeth_ext.logging.setup")
  _base_cls = _setup.BaseLoggingConfig

  caller_file = getattr(mod, "__file__", None) if mod is not None else None
  if caller_file is None:
    caller_file = _find_package_file(module_path)

  # Extract program_name and testing flag from package constants.
  constants: dict[str, object] = {}
  if caller_file is not None:
    constants = parse_and_grab_constants(
      {"PROJECT_NAME": "project_name", "TESTING": "testing"},
      caller_file=caller_file,
    )
  program_name: str = str(constants.get("project_name") or module_path.split(".")[-1])
  testing = bool(constants.get("testing", False))

  # local = file-handler config for the optional private hierarchy.
  local_config, _ = _base_cls.get_default_main_logging_configs(caller_file=caller_file)

  if mode == "main":
    remote_config: dict[str, object] = {}
  else:
    _, remote_config = _base_cls.get_default_socket_logging_configs(caller_file=caller_file)

  return {
    "local": local_config,
    "remote": remote_config,
    "program_name": program_name,
    "testing": testing,
  }


def _query_worker(
  target: str,
  mode: str,
  conn: multiprocessing.connection.Connection,
) -> None:
  """Run inside the spawn subprocess: import *target*, call it, send the result back.

  When *target* uses the default ``get_logging_configs`` function name and that
  attribute is absent from the module, the worker falls back to
  :meth:`~aeth_ext.logging.setup.BaseLoggingConfig.get_default_socket_logging_configs`
  or :meth:`~aeth_ext.logging.setup.BaseLoggingConfig.get_default_main_logging_configs`
  according to *mode*.

  This function is defined at module level so it is importable by its qualified
  name, which is required for pickling under the ``spawn`` start method.
  """
  try:
    result = _resolve_target(target, mode)
  except Exception as exc:  # noqa: BLE001
    try:
      conn.send(exc)
    except Exception:  # noqa: BLE001, S110
      pass  # if the exception itself is not picklable, exit with non-zero code
  else:
    try:
      conn.send(result)
    except Exception as exc:  # noqa: BLE001
      try:
        conn.send(RuntimeError(f"Result from {target!r} is not picklable: {exc}"))
      except Exception:  # noqa: BLE001, S110
        pass
  finally:
    conn.close()
