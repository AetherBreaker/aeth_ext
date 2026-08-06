# Standard library imports
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

# Third party imports
from aiologic import Event

# First party imports
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.logging.config import DictConfigurator

# Everything the single writer thread pulls from the shared queue: either a log
# record to dispatch (a received program record or the server's own record) or a
# client-hierarchy lifecycle event to apply.
type WriterItem = TaggedLogRecord | RegisterClient | UnregisterClient


class ApplyResultEvent(Event):
  """Tri-state (unset/success/failure) signal for a `RegisterClient`'s apply outcome (D-E3).

  A direct :class:`aiologic.Event` subclass that pairs the event with which of
  the two terminal states it was set to - no new synchronization primitive is
  invented, and the rest of :class:`~aiologic.Event`'s interface (``is_set``,
  ``__bool__``, ``__await__``) is inherited rather than re-wrapped. Settable
  exactly once, from the writer thread (:meth:`set_success`/:meth:`set_failure`);
  awaited from the reader's per-connection loop via :meth:`wait_outcome`. The reader
  never waits on this for the `HandshakeAck` itself (D-E2) - only for the
  out-of-band success/failure message sent afterward, once the writer has
  actually applied (or failed to apply) the config.
  """

  __slots__ = ("outcome",)

  def __init__(self) -> None:
    super().__init__()
    self.outcome: Literal["success", "failure"] | None = None

  def set_success(self) -> None:
    self.outcome = "success"
    self.set()

  def set_failure(self) -> None:
    self.outcome = "failure"
    self.set()

  async def wait_outcome(self) -> Literal["success", "failure"]:
    """Await this event's terminal outcome. Must not be called before one is set."""
    await self
    assert self.outcome is not None, "ApplyResultEvent.wait_outcome() resolved before an outcome was set"
    return self.outcome


@dataclass(frozen=True, slots=True)
class RegisterClient:
  """Event handing a connected program's validated-but-unapplied configurator to the writer thread.

  Enqueued by the asyncio reader once a connection's handshake config has
  passed :meth:`DictConfigurator.validate` - cheap and I/O-free, so it runs
  inline on the reader's loop. The writer thread is the one that calls
  :meth:`DictConfigurator.apply` (in a worker thread, see
  :meth:`~aeth_ext.central_log_server.server.writer_thread.LogWriterThread._register_client`),
  which is where the real disk I/O and the ``logging._lock`` hold happen -
  moving that cost off the reader is the entire point of the two-phase split.
  Because this event travels the same FIFO queue as records, the writer
  finishes adopting the hierarchy before any of that program's records are
  dispatched. ``connection_id`` ties the hierarchy to one specific connection
  so a stale :class:`UnregisterClient` from an earlier connection cannot tear
  down a reconnected client's hierarchy.

  ``apply_result`` is how the writer reports the outcome back to the reader
  without the reader ever blocking on it: the `HandshakeAck` is sent the
  moment validation passes (D-E2), and the reader's per-connection loop only
  starts watching this event afterward, racing it against further record
  reads.
  """

  program_name: str
  configurator: DictConfigurator
  connection_id: int
  apply_result: ApplyResultEvent = field(default_factory=ApplyResultEvent)


@dataclass(frozen=True, slots=True)
class UnregisterClient:
  """Event asking the writer thread to tear a program's hierarchy down.

  Enqueued by the asyncio reader when a connection is lost. Sitting behind every
  record that program already enqueued, it guarantees teardown happens only once
  those in-flight records have been flushed. Ignored if ``connection_id`` no
  longer matches the currently registered hierarchy (i.e. the client already
  reconnected).
  """

  program_name: str
  connection_id: int


class StatsData(TypedDict):
  """The live writer state fields, without a message-``type`` tag.

  Used both as the payload of a standalone :class:`StatsSnapshotPacket` and,
  untagged, as the inline ``snapshot`` field of a :class:`StateEventPacket`.
  """

  connected_programs: list[str]
  current_ids: dict[str, int]
  midnight_ids: dict[str, int]
  midnight_date: str


class StatsSnapshotPacket(StatsData):
  """Live writer state pushed to subscribed web viewers as a ``"stats"`` message.

  Sent as the current snapshot the instant a viewer subscribes and again at the
  writer's idle-drain cadence whenever id stats have advanced.
  """

  type: Literal["stats"]


class StateEventPacket(TypedDict):
  """Connect/disconnect event pushed immediately to subscribed web viewers.

  Emitted the instant the writer thread adopts or tears down a program's
  private hierarchy, so the viewer reflects connection changes without waiting
  for the next periodic stats push. The current :class:`StatsData` travels
  inline in ``snapshot`` so the event is self-contained.
  """

  type: Literal["event"]
  event: Literal["connected", "disconnected"]
  program: str
  snapshot: StatsData
