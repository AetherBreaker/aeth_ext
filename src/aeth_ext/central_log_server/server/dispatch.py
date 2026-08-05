# Standard library imports
import logging
from logging.handlers import QueueHandler
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator
  from typing import Any, Literal

  # Third party imports
  from aiologic import SimpleQueue

  # First party imports
  from aeth_ext.central_log_server._types import WriterItem
  from aeth_ext.logging.bases import TaggedLogRecord

__all__ = ["QueueForwardHandler", "build_fallback_config", "iter_unique_handlers", "shutdown_hierarchy"]


class QueueForwardHandler(QueueHandler):
  """Routes the server's own log records onto the shared writer queue.

  Placed on the root logger so that every record emitted inside the log-server
  process -- whether from asyncio callbacks or ordinary threads -- is forwarded
  to the single writer thread via the same in-process queue that client records
  travel.

  **The queue must be an** :class:`aiologic.SimpleQueue`, never an
  :class:`aiologic.Queue`. This is a correctness requirement, not a preference.
  :meth:`emit` is a synchronous API that ``logging`` calls from whatever thread
  emitted the record - including the asyncio event loop thread, which therefore
  has no way to *await* anything here and must use the thread-blocking
  ``green_put``. The two queue types differ fundamentally in what that costs:

  * :class:`aiologic.Queue` is mutex-based - a single ownership token that every
    put *and* get must acquire. ``green_put`` blocks the calling OS thread, with
    no timeout, whenever that token is held. Release hands the token *directly*
    to the first waiter in FIFO order, and returns before that waiter has
    actually run. So if the token is handed to a parked ``async_put`` task on
    the main loop and the loop thread then emits a record, the loop thread
    blocks forever waiting on a token owned by a coroutine only that same -- now
    blocked -- thread could ever resume. The whole process wedges: no heartbeat,
    no socket reads, no log output, no exception, no alert.
  * :class:`aiologic.SimpleQueue` is semaphore-based. ``green_put`` is an
    unconditional ``deque.append`` plus a semaphore release; there is no token to
    contend for and ``putting`` is always ``0``. A put can never wait on
    anything, so the deadlock above has no first step.

  ``blocking=False`` does **not** mean "drop if full" here - :class:`SimpleQueue`
  is unbounded and has no full state. It only skips aiologic's optional
  green checkpoint (a thread yield, off by default but enabled by the
  ``AIOLOGIC_THREADING_CHECKPOINTS``/``AIOLOGIC_GREEN_CHECKPOINTS`` environment
  variables), so this call site stays free of any thread-yielding behaviour
  regardless of how the process environment is configured. The record is still
  enqueued exactly as it would be otherwise.

  The writer thread dispatches records into *private* logging hierarchies whose
  loggers never propagate to the process-global root, so dispatched records
  cannot re-enter this handler and loop back onto the queue.
  """

  def __init__(self, queue: SimpleQueue[WriterItem]) -> None:
    self.queue: SimpleQueue[WriterItem]  # pyright: ignore[reportIncompatibleVariableOverride]
    super().__init__(queue)  # type: ignore[arg-type]

  @override
  def enqueue(self, record: TaggedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    self.queue.green_put(record, blocking=False)


def iter_unique_handlers(manager: logging.Manager, root: logging.Logger) -> Iterator[logging.Handler]:
  """Yield every handler attached anywhere in a private hierarchy, each exactly once.

  A handler shared across multiple loggers (e.g. attached to both ``root`` and
  a child) would otherwise be visited once per logger; callers such as the
  connect/disconnect separator broadcast want to write to it only once.
  """
  seen: set[int] = set()
  loggers: list[logging.Logger] = [root]
  loggers.extend(node for node in manager.loggerDict.values() if isinstance(node, logging.Logger))
  for node in loggers:
    for handler in node.handlers:
      if id(handler) in seen:
        continue
      seen.add(id(handler))
      yield handler


def shutdown_hierarchy(manager: logging.Manager, root: logging.Logger) -> None:
  """Detach, flush, and close every handler attached anywhere in a private hierarchy.

  Mirrors ``logging.shutdown`` for a single private hierarchy: flush/close
  errors are swallowed because at teardown there is nothing useful left to do
  with them (the underlying stream may already be gone).
  """
  closed: set[int] = set()
  loggers: list[logging.Logger] = [root]
  loggers.extend(node for node in manager.loggerDict.values() if isinstance(node, logging.Logger))
  for node in loggers:
    for handler in node.handlers[:]:
      node.removeHandler(handler)
      if id(handler) in closed:
        continue
      closed.add(id(handler))
      try:
        handler.flush()
        handler.close()
      except OSError, RuntimeError:
        pass


def build_fallback_config(program_name: str, logging_type: Literal["daily", "per_run"]) -> dict[str, Any]:
  """Build a known-good, minimal logging config for a program whose remote config failed to apply (D-D1).

  Deliberately reuses none of the failed config's own handlers/formatters/filters
  - since *something* in that config is what caused the apply failure, the
  fallback falls back to the same packaged handler classes and formatter used
  by the ``remote_daily``/``remote_per_run`` defaults, matching *logging_type*
  so the degraded output keeps the rotation style the client declared (D-D2).
  Files are suffixed ``_fallback`` (D-D3) so a degraded program is obvious on
  disk; ``logdir://`` resolves them under the caller's ``log_dir`` exactly like
  any other config, so this still requires apply()'s one-pass mkdir.
  """
  handler_base: dict[str, Any] = (
    {
      "class": "aeth_ext.logging.bases.CustomTimedRotatingFileHandler",
      "when": "midnight",
      "backupCount": 14,
      "delay": True,
    }
    if logging_type == "daily"
    else {
      "()": "aeth_ext.logging.setup.make_per_run_file_handler",
      "backupCount": 30,
    }
  )
  return {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
      "preferred": {
        "()": "aeth_ext.logging.bases.SmartColumnFormatter",
        "columns": ["{libpath}", "[{asctime}]", "{levelname: >8}", "{message}"],
        "separator": " | ",
        "datefmt": "%b, %d %y - %a %I:%M %p",
      }
    },
    "handlers": {
      "debug_file": {
        **handler_base,
        "filename": f"logdir://{program_name}_fallback_debug.log",
        "level": "DEBUG",
        "formatter": "preferred",
      },
      "info_file": {
        **handler_base,
        "filename": f"logdir://{program_name}_fallback.log",
        "level": "INFO",
        "formatter": "preferred",
      },
    },
    "root": {"level": "DEBUG", "handlers": ["debug_file", "info_file"]},
  }
