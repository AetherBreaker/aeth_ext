# Standard library imports
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from datetime import datetime

  # First party imports
  from aeth_ext.types import SizedBuffer


__all__ = ["HandleProvider", "ListDirResult"]

type BufferSize = int
type TransferSuccess = bool
type InstrumentCallable = Callable[[SizedBuffer], Any]
type ReadCallback = Callable[[BufferSize], bytes]
type WriteCallback = Callable[[SizedBuffer], Any]


class ListDirResult(NamedTuple):
  filename: str
  modified_time: datetime


class HandleProvider[HandleT](Protocol):
  """Narrow extension point: something that can hand out a connection handle and take it back.

  `FTPAdapter` (`AdaptedFTP`'s provider) and `SFTPChannelPool` (`AdaptedSFTP`'s; `SFTPAdapter` itself
  has neither method) structurally satisfy this via their own public `acquire`/`release` methods --
  says nothing about *how* a handle is obtained, only that something can provide and reclaim one.
  Lets a consumer construct `AdaptedFTP`/`AdaptedSFTP` directly with a hand-written provider for
  one-shot, non-pooled usage, entirely bypassing `FTPAdapter`/`create_ftp_adapter`.
  """

  __slots__ = ()

  def acquire(self) -> tuple[HandleT, Sequence[InstrumentCallable]]:
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
