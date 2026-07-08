# Standard library imports
import logging
from dataclasses import dataclass
from logging.handlers import QueueHandler
from typing import TYPE_CHECKING, override

# First party imports

if TYPE_CHECKING:
  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.central_log_server.protocol import LoggingHandshake, TaggedLogRecord


# Everything the single writer thread pulls from the shared queue: either a log
# record to dispatch (a received program record or the server's own record) or a
# handler-lifecycle event to apply.
type WriterItem = TaggedLogRecord | RegisterHandlers | UnregisterHandlers


class ProgramFilter(logging.Filter):
  """Passes only records stamped with a matching ``source_name``.

  Attached to a connected program's dedicated handlers so that the single
  dispatch logger can hold the handlers of every program at once while ordinary
  logging filtering keeps each program's records flowing only to its own files.
  """

  def __init__(self, program_name: str) -> None:
    super().__init__()
    self.program_name: str = program_name

  @override
  def filter(self, record: TaggedLogRecord) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
    return getattr(record, "source_name", None) == self.program_name


class ServerFilter(logging.Filter):
  """Passes only the log processor's *own* records.

  The server's records are produced by ordinary logging (via the root
  ``QueueForwardHandler``) and therefore carry no ``source_name``, unlike the
  program records decoded off a socket. Attaching this to the server's own file
  and console handlers keeps received program records out of the server's logs.
  """

  @override
  def filter(self, record: TaggedLogRecord) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
    return getattr(record, "source_name", None) is None


class QueueForwardHandler(QueueHandler):
  """Routes the server's own log records onto the shared writer queue.

  Placed on the root logger so that every record emitted inside the log-server
  process -- whether from asyncio callbacks or ordinary threads -- is forwarded
  to the single writer thread via the same in-process queue that client records
  travel. Because the queue is unbounded and :meth:`enqueue` is non-blocking,
  calls from the asyncio event loop complete without suspending the loop.

  ``DISPATCH_LOGGER`` sets ``propagate=False``, so dispatched records never
  re-enter this handler and cannot loop back onto the queue.
  """

  def __init__(self, queue: Queue[WriterItem]) -> None:
    self.queue: Queue[WriterItem]  # pyright: ignore[reportIncompatibleVariableOverride]
    super().__init__(queue)  # type: ignore[arg-type]

  @override
  def enqueue(self, record: TaggedLogRecord) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
    self.queue.green_put(record)


@dataclass(frozen=True, slots=True)
class RegisterHandlers:
  """Event asking the writer thread to register a program's handlers.

  Enqueued by the asyncio reader when a connection's handshake arrives. Because
  it travels the same FIFO queue as records, the writer applies it before any of
  that program's records are dispatched.
  """

  handshake: LoggingHandshake


@dataclass(frozen=True, slots=True)
class UnregisterHandlers:
  """Event asking the writer thread to tear a program's handlers down.

  Enqueued by the asyncio reader when a connection is lost. Sitting behind every
  record that program already enqueued, it guarantees teardown happens only once
  those in-flight records have been flushed.
  """

  program_name: str


# A dedicated, non-propagating logger that owns the handlers of every connected
# program plus the server's own handlers. propagate=False keeps dispatched
# records out of the root handlers (which would re-enqueue them via
# QueueForwardHandler and loop).
DISPATCH_LOGGER: logging.Logger = logging.getLogger("aeth_ext.shared_log_processor.dispatch")
DISPATCH_LOGGER.propagate = False
