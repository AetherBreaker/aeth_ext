# Standard library imports
import logging
from logging.handlers import QueueHandler
from typing import TYPE_CHECKING, override

# First party imports
from aeth_ext.logging.config import dict_config

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from pathlib import Path
  from typing import Any

  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.central_log_server._types import WriterItem
  from aeth_ext.logging.bases import TaggedLogRecord

__all__ = ["QueueForwardHandler", "build_hierarchy", "shutdown_hierarchy"]


class QueueForwardHandler(QueueHandler):
  """Routes the server's own log records onto the shared writer queue.

  Placed on the root logger so that every record emitted inside the log-server
  process -- whether from asyncio callbacks or ordinary threads -- is forwarded
  to the single writer thread via the same in-process queue that client records
  travel. Because the queue is unbounded and :meth:`enqueue` is non-blocking,
  calls from the asyncio event loop complete without suspending the loop.

  The writer thread dispatches records into *private* logging hierarchies whose
  loggers never propagate to the process-global root, so dispatched records
  cannot re-enter this handler and loop back onto the queue.
  """

  def __init__(self, queue: Queue[WriterItem]) -> None:
    self.queue: Queue[WriterItem]  # pyright: ignore[reportIncompatibleVariableOverride]
    super().__init__(queue)  # type: ignore[arg-type]

  @override
  def enqueue(self, record: TaggedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    self.queue.green_put(record)


def build_hierarchy(config: Mapping[str, Any], log_dir: Path) -> tuple[logging.Manager, logging.Logger]:
  """Build a private logging hierarchy and apply *config* into it.

  Returns the new hierarchy's manager and root logger. ``logdir://`` values in
  *config* are resolved beneath *log_dir*. Raises (typically ``ValueError``
  from the configurator) if the config is invalid or cannot be applied, so a
  bad remote config can be rejected at handshake time.
  """
  root = logging.RootLogger(logging.WARNING)
  manager = logging.Manager(root)
  # Manager(root) does not point the root back at the manager; without this,
  # loggers reached via the root (e.g. ``root.getChild``) and the root's own
  # ``isEnabledFor`` would consult the process-global manager instead.
  root.manager = manager
  dict_config(config, manager=manager, root=root, log_dir=log_dir)
  return manager, root


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
      except Exception:
        pass
