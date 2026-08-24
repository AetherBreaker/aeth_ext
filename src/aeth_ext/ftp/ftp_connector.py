"""Credentials-driven connection-opening logic for `FTPAdapter`.

Not a public extension point -- `HandleProvider` (`aeth_ext.ftp.types`) is. `FTPAdapter` builds
exactly one `FTPConnector` from its credentials and holds it for its whole lifetime. Split into its
own module (separate from `sftp_connector.py`) so importing/using plain FTP support never requires
`paramiko`, which is only guaranteed present when the optional `sftp` extra is installed. Not
underscore-prefixed despite being outside the public API (absent from `__all__`) -- `FTPConnector`
is constructed from `aeth_ext.ftp.pool.ftp_adapter`, and pyright's `reportPrivateUsage` flags any
cross-module reference to a leading-underscore name, not just cross-class attribute access;
omission from `__all__` alone signals "internal" without tripping it.
"""

# Standard library imports
import ssl
from ftplib import FTP, FTP_TLS, error_temp
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.ftp.errors import ServerCapacityError, ServerNotAvailableError

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.ftp.credentials import FTPCredentials


_CAPACITY_REFUSAL_MARKERS = (
  "too many",
  "maximum number of",
  "connection limit",
  "max clients",
)
"""Substrings (checked case-insensitively) that appear in real FTP daemons' 421 reply text
specifically for a hit connection-count limit -- as opposed to the many other reasons a server sends
a 421 (maintenance, idle timeout, overload). A 421 without one of these is treated as an ordinary
transient error_temp, not a discovered server ceiling.

Matched loosely on purpose. These were originally spelled as whole phrases ("too many connections",
"too many users") and matched *nothing*: vsftpd 3.0.5 -- verified live -- answers `421 There are too
many connected users, please try later.`, in which neither phrase appears as a substring. Ceiling
discovery was therefore dead against the most widely deployed FTP daemon there is, and every caller
saw a bare error_temp instead of the pool backing off. The wording varies per daemon and version
("too many connected users", "Too many users are connected", "the maximum number of clients (N)..."),
so match the stable fragment rather than any one vendor's full sentence. False positives are bounded:
`request_handler` only consults these for a reply that already starts with 421."""


class FTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: FTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The FTP server credentials to connect with.
    """
    self._credentials = credentials

  def get_transport(self) -> None:
    """No-op: plain FTP has no separate transport tier to open."""
    return  # no-op: FTP has no separate transport/channel tiers

  def close_transport_handler(self, handle: None) -> None:
    """No-op: plain FTP has no separate transport tier to close."""
    return  # no-op: FTP has no separate transport/channel tiers

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    """Opens and authenticates a new `FTP`/`FTP_TLS` connection.

    Accepts and ignores positional/keyword arguments so its call signature matches
    `SFTPConnector.request_handler`'s (which takes the transport to open a channel on) --
    FTP has no such transport argument to pass.

    Returns:
      The connected, authenticated handle.
    """
    if self._credentials.use_tls:
      context = ssl.create_default_context()
      if not self._credentials.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
      conn = FTP_TLS(context=context)
    else:
      conn = FTP()
    try:
      try:
        conn.connect(
          self._credentials.host,
          self._credentials.port,
          timeout=self._credentials.connect_timeout,  # pyright: ignore[reportArgumentType] -- ftplib's stub omits `| None` even though the real default is None
        )
      except OSError as e:
        # The server never responded at the socket level (refused, timed out, DNS failure, network
        # down) -- distinct from a live server actively refusing via an FTP-level reply (error_temp
        # below, from a rejected greeting). This is the documented public contract (README): callers
        # catch ServerNotAvailableError specifically to mean "the server is unreachable," which
        # nothing previously raised -- connect() failures propagated as a raw OSError instead.
        raise ServerNotAvailableError(str(e)) from e
      conn.login(self._credentials.username, self._credentials.password.get_secret_value())
      if isinstance(conn, FTP_TLS) and self._credentials.protect_data_channel is not False:
        conn.prot_p()
      conn.set_pasv(self._credentials.passive_mode)
    except error_temp as e:
      # A 421 reply means only "service not available, closing control connection" -- servers also
      # send it for unrelated temporary shutdowns/maintenance/overload, not just a hit connection
      # limit. Blindly treating every 421 as a capacity signal would pin _discovered_max to the
      # current pool size for a full _REPROBE_INTERVAL after an unrelated outage. Only classify it as
      # ServerCapacityError when the reply text itself gives positive evidence of a connection-count
      # limit, matching common server wording; anything else stays a plain error_temp.
      conn.close()
      reply = str(e)
      if reply.startswith("421") and any(marker in reply.lower() for marker in _CAPACITY_REFUSAL_MARKERS):
        raise ServerCapacityError(reply) from e
      raise
    except BaseException:
      # ftplib opens the socket in connect() and never closes it on a later failure (nor on its own
      # failure after create_connection succeeded, e.g. a 421 greeting), and FTP has no __del__ --
      # without this, a run of bad logins or TLS failures leaks one socket each until the process
      # exits. close() is idempotent and safe when connect() never got as far as a socket.
      conn.close()
      raise
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    """Closes a connection with a raw socket close, not a graceful `QUIT`.

    Args:
      handle: The connection to close.
    """
    # Every call site is pooled cleanup (discarding a broken/idle handle, or shutdown teardown),
    # never an in-use session's own graceful sign-off -- so there's no reply worth waiting for.
    # quit() sends QUIT and blocks for the server's reply with no timeout (connect_timeout only
    # covers connect()), so an unresponsive server would wedge whichever of these paths called it,
    # including _shutdown_teardown()'s own time budget. close() just drops the socket.
    handle.close()
