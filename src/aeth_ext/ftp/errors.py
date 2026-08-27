__all__ = [
  "HandleReleasedError",
  "LookupUnavailableError",
  "MalformedReplyError",
  "PoolClosedError",
  "PoolTimeoutError",
  "ServerCapacityError",
  "ServerNotAvailableError",
]


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


class LookupUnavailableError(OSError):
  """Raised when the server refused or could not answer an existence/size lookup, leaving the file's
  existence genuinely unknown -- as opposed to confirming it absent.

  An FTP `550` covers both "no such file" and "`SIZE` is disabled or the connection is in ASCII
  mode", and paramiko raises a bare errno-less `OSError` for any SFTP status it has no mapping for.
  Folding either into `FileNotFoundError` would report every path as absent on such a server, so
  callers using a size lookup as an existence probe would take their "not there" branch every time
  and never reach duplicate handling. Absence must be confirmed, never inferred from a refusal.

  An `OSError` so that callers who broadly catch one still handle it, but deliberately *not* a
  `FileNotFoundError`: the distinction it exists to preserve is exactly that. Not a
  `ConnectionError` either -- the connection is healthy and reusable, only this one command failed,
  so the pooled handle must not be discarded on its account.

  This signals a server/configuration problem rather than a caller mistake. Retrying the identical
  call against the same server will usually fail identically.
  """


class MalformedReplyError(OSError):
  """Raised when the server's reply to a lookup could not be parsed as the protocol requires -- an
  FTP `SIZE` reply with a non-`213` code, or a paramiko `SFTPError`.

  Distinct from `LookupUnavailableError`: that is the server coherently declining to answer, while
  this is a reply that did not conform at all. Both leave existence unknown, and both are aeth_ext's
  or the server's problem rather than the caller's, but they warrant different investigation.

  Previously absorbed into a `None` return by `AdaptedSFTP.get_size`, which silently conflated "the
  server misbehaved" with a real answer. Raising keeps the failure visible.
  """
