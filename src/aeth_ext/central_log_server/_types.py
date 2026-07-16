# Standard library imports
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

# First party imports
from aeth_ext.logging.bases import TaggedLogRecord

if TYPE_CHECKING:
  # Standard library imports
  from logging import Logger, Manager

# Everything the single writer thread pulls from the shared queue: either a log
# record to dispatch (a received program record or the server's own record) or a
# client-hierarchy lifecycle event to apply.
type WriterItem = TaggedLogRecord | RegisterClient | UnregisterClient


@dataclass(frozen=True, slots=True)
class RegisterClient:
  """Event handing a connected program's freshly built private hierarchy to the writer thread.

  Enqueued by the asyncio reader once a connection's handshake config has been
  validated and applied via :func:`build_hierarchy`. Because it travels the
  same FIFO queue as records, the writer adopts the hierarchy before any of
  that program's records are dispatched. ``connection_id`` ties the hierarchy
  to one specific connection so a stale :class:`UnregisterClient` from an
  earlier connection cannot tear down a reconnected client's hierarchy.
  """

  program_name: str
  manager: Manager
  root: Logger
  connection_id: int


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
