__all__ = ["PoolClosedError", "ServerCapacityError", "ServerNotAvailableError"]


class ServerNotAvailableError(ConnectionError):
  """Raised when a connector's dial fails at the socket level -- refused, timed out, DNS failure, or
  network unreachable -- rather than the server rejecting credentials or a host key once reached.

  Distinct from `ServerCapacityError`: that means a live server explicitly refused for a
  resource/connection-count reason, while this means the server was never reached at all.
  """


class ServerCapacityError(ConnectionError):
  """Raised by a connector's dial when the server explicitly refuses a new connection because it is
  at some resource/connection-count limit (e.g. an FTP `421` reply) -- as opposed to a transient or
  unrelated connectivity failure (a timeout, reset, DNS failure, or network outage).

  Only this signals a real server-side ceiling to `PooledAdapterBase._open_new_slot`: a bare
  `OSError` is not evidence of one and must not cap the pool for `_REPROBE_INTERVAL` on that basis.
  """


class PoolClosedError(RuntimeError):
  """Raised when a connection is requested from a pool whose shutdown teardown has already run.

  Deliberately not a `ConnectionError`: consumers catch that to ride out transient server failures,
  and a torn-down pool never recovers -- retrying one would spin until the process exits. Only
  `acquire` raises this; `release` keeps working after teardown so sessions checked out beforehand
  can run to completion.
  """
