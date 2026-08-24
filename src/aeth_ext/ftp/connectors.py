"""Credentials-driven connection-opening logic for `FTPAdapter`/`SFTPAdapter`.

Not a public extension point -- `HandleProvider` (`aeth_ext.ftp.types`) is. Each `FTPAdapter`/
`SFTPAdapter` builds exactly one of `FTPConnector`/`SFTPConnector` from its credentials and holds it
for its whole lifetime. Not underscore-prefixed despite being outside the public API (absent from
`__all__`) -- both are constructed from other modules in `aeth_ext.ftp.pool`, and pyright's
`reportPrivateUsage` flags any cross-module reference to a leading-underscore name, not just
cross-class attribute access; omission from `__all__` alone signals "internal" without tripping it.
"""

# Standard library imports
import ssl
from ftplib import FTP, FTP_TLS, error_temp
from logging import getLogger
from typing import TYPE_CHECKING

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SSHClient

# First party imports
from aeth_ext.ftp.errors import ServerCapacityError, ServerNotAvailableError

if TYPE_CHECKING:
  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials


logger = getLogger(__name__)

_CAPACITY_REFUSAL_MARKERS = (
  "too many connections",
  "too many users",
  "maximum number of",
  "connection limit",
)
"""Substrings (checked case-insensitively) that appear in real FTP daemons' 421 reply text
specifically for a hit connection-count limit (vsftpd, ProFTPD, Pure-FTPd, ...) -- as opposed to the
many other reasons a server sends a 421 (maintenance, idle timeout, overload). A 421 without one of
these is treated as an ordinary transient error_temp, not a discovered server ceiling."""


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


class SFTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: SFTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The SFTP server credentials to connect with.

    Raises:
      FileNotFoundError: `credentials.private_key_path` is set but doesn't exist.
      IsADirectoryError: `credentials.private_key_path` is set but is a directory (POSIX).
      PermissionError: `credentials.private_key_path` is set but isn't readable, or (on Windows) is
        a directory.
    """
    if credentials.private_key_path is not None:
      # Paramiko loads this file during _auth(), strictly after connect()'s own socket/host-key work
      # has already succeeded, inside a handler that only catches SSHException -- a missing, directory,
      # or unreadable key file raises a bare FileNotFoundError/IsADirectoryError/PermissionError there,
      # which would otherwise fall straight into get_transport()'s except OSError and get mislabeled as
      # "server unreachable." A plain .stat() wouldn't catch all three (a directory or a
      # permission-denied file both pass it), so actually open the file instead -- once here at
      # construction, not per get_transport() call, since this connector is built once per adapter
      # (see this module's docstring) and reused for the adapter's whole lifetime.
      credentials.private_key_path.open("rb").close()
    self._credentials = credentials

  def get_transport(self) -> Transport:
    """Opens and authenticates a new SSH `Transport`, applying the configured host-key policy.

    Returns:
      The connected, authenticated `Transport`.
    """
    client = SSHClient()
    if self._credentials.known_hosts_path is not None:
      client.load_host_keys(str(self._credentials.known_hosts_path))
    else:
      client.load_system_host_keys()
    client.set_missing_host_key_policy(AutoAddPolicy() if self._credentials.host_key_policy == "auto_add" else RejectPolicy())
    try:
      try:
        client.connect(
          self._credentials.host,
          port=self._credentials.port,
          username=self._credentials.username,
          password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
          key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
          passphrase=(
            self._credentials.private_key_passphrase.get_secret_value()
            if self._credentials.private_key_passphrase is not None
            else None
          ),
          timeout=self._credentials.connect_timeout,
          # SFTPCredentials always requires an explicit password or private_key_path (see its
          # model validator) -- Paramiko's own defaults (allow_agent=True, look_for_keys=True)
          # would otherwise let a caller who configured one identity silently authenticate with
          # a different one from an ambient SSH agent or ~/.ssh key instead.
          allow_agent=False,
          look_for_keys=False,
        )
      except OSError as e:
        # Every socket-level connect failure (refused, timed out, DNS failure, network down) is an
        # OSError -- paramiko's own NoValidConnectionsError (raised once every candidate address has
        # been exhausted) is deliberately an OSError subclass for exactly this reason. Distinct from
        # AuthenticationException/BadHostKeyException/SSHException below, which mean the server *was*
        # reached but rejected credentials or a host key -- those keep propagating unchanged. This is
        # the documented public contract (README): callers catch ServerNotAvailableError specifically
        # to mean "the server is unreachable," which nothing previously raised.
        if self._credentials.private_key_path is not None:
          # __init__ verified this file was readable once, but this connector is built once per
          # adapter and reused for its whole lifetime (see this module's docstring) -- if the file
          # disappeared or lost permissions since, paramiko's own FileNotFoundError/IsADirectoryError/
          # PermissionError from _auth() is indistinguishable from a genuine socket-level OSError here.
          # Re-check now so that failure surfaces as itself instead of being misclassified below.
          self._credentials.private_key_path.open("rb").close()
        raise ServerNotAvailableError(str(e)) from e
      transport = client.get_transport()
      assert transport is not None
    except BaseException:
      # SSHClient.connect() builds (and starts the background thread of) a Transport before host-key
      # verification and authentication run, and cleans none of it up when either rejects us; SSHClient
      # has no __del__, and a live Transport thread keeps itself reachable, so the socket and thread
      # would outlive every failed dial. Only reached on failure -- the returned Transport is
      # deliberately kept alive past the client that opened it, and client.close() would close it too.
      client.close()
      raise
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    """Opens a new SFTP channel.

    Args:
      transport: The `Transport` to open a channel on.

    Returns:
      The new channel.
    """
    return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

  def close_conn_handler(self, handle: SFTPClient) -> None:
    """Best-effort close of a single SFTP channel, swallowing any error.

    Args:
      handle: The channel to close.
    """
    try:
      handle.close()
    except Exception as e:
      logger.warning("Error closing SFTP channel handle: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP channel handle close error", exc_info=e)

  def close_transport_handler(self, handle: Transport) -> None:
    """Best-effort close of the whole `Transport` (and every channel opened on it), swallowing any error.

    Args:
      handle: The `Transport` to close.
    """
    try:
      handle.close()
    except Exception as e:
      logger.warning("Error closing SFTP transport: %s: %s", type(e).__name__, e)
      logger.debug("Traceback for SFTP transport close error", exc_info=e)
