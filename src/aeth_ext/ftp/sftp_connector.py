"""Credentials-driven connection-opening logic for `SFTPAdapter`.

Not a public extension point -- `HandleProvider` (`aeth_ext.ftp.types`) is. `SFTPAdapter` builds
exactly one `SFTPConnector` from its credentials and holds it for its whole lifetime. Split into its
own module (separate from `ftp_connector.py`) so importing/using plain FTP support never requires
this module's `paramiko` dependency, which is only guaranteed present when the optional `sftp`
extra is installed. Not underscore-prefixed despite being outside the public API (absent from
`__all__`) -- `SFTPConnector` is constructed from `aeth_ext.ftp.pool.sftp_adapter`, and pyright's
`reportPrivateUsage` flags any cross-module reference to a leading-underscore name, not just
cross-class attribute access; omission from `__all__` alone signals "internal" without tripping it.
"""

# Standard library imports
from logging import getLogger
from typing import TYPE_CHECKING

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SSHClient

# First party imports
from aeth_ext.ftp.errors import ServerNotAvailableError

if TYPE_CHECKING:
  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.ftp.credentials import SFTPCredentials


logger = getLogger(__name__)


class SFTPConnector:
  """Opens, authenticates, and closes SSH `Transport`s and SFTP channels from one set of credentials."""

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
