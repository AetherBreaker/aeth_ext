__all__ = ["PoolClosedError", "ServerNotAvailableError"]


class ServerNotAvailableError(ConnectionError):
  pass


class PoolClosedError(RuntimeError):
  """Raised when a connection is requested from a pool whose shutdown teardown has already run.

  Deliberately not a `ConnectionError`: consumers catch that to ride out transient server failures,
  and a torn-down pool never recovers -- retrying one would spin until the process exits. Only
  `acquire` raises this; `release` keeps working after teardown so sessions checked out beforehand
  can run to completion.
  """
