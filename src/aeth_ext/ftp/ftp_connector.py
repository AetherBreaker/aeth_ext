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


_NON_CAPACITY_MARKERS = (
  "maintenance",
  "going down",
  "shutting down",
  "shut down",
  "restarting",
  "reboot",
)
"""Substrings (checked case-insensitively) naming a *planned outage* -- the one class of 421 that a
pool should not read as "the server is at its connection ceiling". Every other 421 raised while
dialing is treated as a capacity refusal.

Deliberately a deny-list, having been an allow-list of capacity wordings and failed badly. vsftpd
3.0.5 answers `421 There are too many connected users, please try later.`, which the allow-list's
whole phrases ("too many connections", "too many users") did not contain, so `_discovered_max` was
never pinned and ceiling discovery sat switched off against the most widely deployed FTP daemon
there is -- silently, for every caller, forever.

Inverting it fixes the direction the mistake falls in. The two error costs are wildly asymmetric: a
capacity refusal missed disables ramp-up permanently and invisibly, whereas a planned outage read as
capacity merely caps the pool at its current size until the next re-probe, which then lifts it
again. A wording this list fails to anticipate therefore costs a slightly less specific error, not a
broken pool.

Two properties keep the blanket treatment honest. These are consulted only while opening a *new*
connection, so an idle-timeout 421 cannot reach here; and `PooledAdapterBase._open_new_slot` refuses
to pin a ceiling when the rollback leaves `_current_size` at zero, so a total outage propagates
instead of capping the pool at nothing. What the list buys is diagnostics: a server that says why it
is unavailable gets that reason back to the caller intact, rather than flattened into an eventual
`PoolTimeoutError` that would read as "pool saturated"."""


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
      # `TYPE` is sticky for the life of the control connection, so binary is set once here rather
      # than per transfer. Every transfer path assumes it; switch modes only for the duration of the
      # work that needs it and restore `TYPE I` before releasing the handle.
      conn.voidcmd("TYPE I")
    except error_temp as e:
      # A 421 while dialing means the server turned this connection away. Read that as a capacity
      # refusal unless it names a planned outage: connection limits, rate limits and overload all
      # want the same response (stop growing, wait for a slot), and the wording daemons use for them
      # varies far too much to enumerate -- see _NON_CAPACITY_MARKERS for what enumerating it cost.
      # Anything naming maintenance or a shutdown stays a plain error_temp so its reason survives.
      conn.close()
      reply = str(e)
      if reply.startswith("421") and not any(marker in reply.lower() for marker in _NON_CAPACITY_MARKERS):
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
