# Standard library imports
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Sequence
  from datetime import datetime
  from typing import Any


__all__ = ["HandleProvider", "ListDirResult", "TransportProvider"]

type BufferSize = int
type TransferSuccess = bool
type IntrumentCallable = Callable[[bytes], Any]
type ReadCallback = Callable[[BufferSize], Any]
type WriteCallback = Callable[[bytes], Any]


class ListDirResult(NamedTuple):
  filename: str
  modified_time: datetime


class HandleProvider[HandleT](Protocol):
  """Narrow extension point: something that can hand out a connection handle and take it back.

  `FTPAdapter`/`SFTPAdapter` structurally satisfy this via their own public `acquire`/`release` methods
  -- says nothing about *how* a handle is obtained, only that something can provide and reclaim one.
  Lets a consumer construct `AdaptedFTP`/`AdaptedSFTP` directly with a hand-written provider for
  one-shot, non-pooled usage, entirely bypassing `FTPAdapter`/`create_ftp_adapter`.
  """

  __slots__ = ()

  def acquire(self) -> tuple[HandleT, Sequence[Callable[[bytes], Any]]]:
    """Acquires a connection handle.

    Returns:
      The handle, plus any handle-scoped observer callbacks to attach to it.
    """
    ...

  def release(self, handle: HandleT, is_fatal: bool) -> None:
    """Returns a handle previously acquired via `acquire`.

    Args:
      handle: The handle to return.
      is_fatal: Whether the connection is broken and should be discarded rather than reused.
    """
    ...


class TransportProvider[TransportT](Protocol):
  """Narrow extension point: something that can dial a brand new low-level transport (a `Transport`,
  conceptually a TCP+SSH handshake) within its own connection-count ceiling, and be told when one died.

  `SFTPAdapter` structurally satisfies this via its own `open_transport`/`transport_dropped` methods --
  lets `SFTPChannelPool` grow the pool without holding a direct reference to `SFTPAdapter`.
  """

  __slots__ = ()

  def open_transport(self) -> TransportT | None:
    """Dials a new low-level transport within the provider's connection-count ceiling.

    Returns:
      The new transport, or `None` if the ceiling was already reached.
    """
    ...

  def transport_dropped(self) -> None:
    """Records that a previously-opened transport has died, freeing one slot in the ceiling."""
    ...
