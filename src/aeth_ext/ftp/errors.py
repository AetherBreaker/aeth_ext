"""Exceptions raised by the FTP/SFTP connectors, pools, and sessions."""

__all__ = ["HandleReleasedError", "PoolClosedError", "PoolTimeoutError", "ServerCapacityError", "ServerNotAvailableError"]


class ServerNotAvailableError(ConnectionError):
  """Raised when a connector's dial fails at the socket level.

  Refused, timed out, DNS failure, or network unreachable -- rather than the server rejecting
  credentials or a host key once reached.

  Distinct from `ServerCapacityError`: that means a live server explicitly refused for a
  resource/connection-count reason, while this means the server was never reached at all.
  """


class ServerCapacityError(ConnectionError):
  """Raised by a connector's dial when the server explicitly refuses a new connection for capacity reasons.

  That is, a resource/connection-count limit (e.g. an FTP `421` reply) -- as opposed to a transient
  or unrelated connectivity failure (a timeout, reset, DNS failure, or network outage).

  Only this signals a real server-side ceiling to `PooledAdapterBase._open_new_slot`: a bare
  `OSError` is not evidence of one and must not cap the pool for `_REPROBE_INTERVAL` on that basis.
  """


class PoolTimeoutError(TimeoutError):
  """Raised when `acquire_timeout` elapses before a pooled connection could be checked out.

  A `TimeoutError` (and so an `OSError`) rather than a `ConnectionError`: nothing is wrong with the
  server or with any individual connection -- the pool simply stayed at capacity for longer than the
  caller was willing to wait. Retrying is entirely reasonable, unlike `PoolClosedError`.
  """


class HandleReleasedError(RuntimeError):
  """Raised when a lazily-consumed result is advanced after its session gave the handle back.

  `listdir` streams from the live connection as it is iterated, so an iterator kept past its
  session's `with` block would read from a handle the pool has already handed to someone else --
  interleaving two callers' traffic on one connection. Materializing the whole listing instead would
  cap memory at the size of the directory, so the iterator stays lazy and reports the misuse
  directly.

  Not a `ConnectionError`: this is a caller-lifecycle mistake, not a server or network failure, and
  retrying without restructuring the call would fail identically.
  """


class PoolClosedError(RuntimeError):
  """Raised when a connection is requested from a pool whose shutdown teardown has already run.

  Deliberately not a `ConnectionError`: consumers catch that to ride out transient server failures,
  and a torn-down pool never recovers -- retrying one would spin until the process exits. Only
  `acquire` raises this; `release` keeps working after teardown so sessions checked out beforehand
  can run to completion.
  """
