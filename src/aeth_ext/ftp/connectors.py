"""Credentials-driven connection-opening logic for `FTPAdapter`/`SFTPAdapter`.

Not a public extension point -- `HandleProvider` (`aeth_ext.ftp.types`) is. Each `FTPAdapter`/
`SFTPAdapter` builds exactly one of `FTPConnector`/`SFTPConnector` from its credentials and holds it
for its whole lifetime. Not underscore-prefixed despite being outside the public API (absent from
`__all__`) -- both are constructed from other modules in `aeth_ext.ftp.pool`, and pyright's
`reportPrivateUsage` flags any cross-module reference to a leading-underscore name, not just
cross-class attribute access; omission from `__all__` alone signals "internal" without tripping it.
"""

# Standard library imports
from ftplib import FTP, FTP_TLS
from typing import TYPE_CHECKING

# Third party imports
from paramiko import AutoAddPolicy, RejectPolicy, SFTPClient, SSHClient

if TYPE_CHECKING:
  # Third party imports
  from paramiko import Transport

  # First party imports
  from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials


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

  def request_handler(self, *_args: object, **_kwargs: object) -> FTP:
    """Opens and authenticates a new `FTP`/`FTP_TLS` connection.

    Accepts and ignores positional/keyword arguments so its call signature matches
    `SFTPConnector.request_handler`'s (which takes the transport to open a channel on) --
    FTP has no such transport argument to pass.

    Returns:
      The connected, authenticated handle.
    """
    conn = FTP_TLS() if self._credentials.use_tls else FTP()
    conn.connect(
      self._credentials.host,
      self._credentials.port,
      timeout=self._credentials.connect_timeout,  # pyright: ignore[reportArgumentType] -- ftplib's stub omits `| None` even though the real default is None
    )
    conn.login(self._credentials.username, self._credentials.password.get_secret_value())
    conn.set_pasv(self._credentials.passive_mode)
    return conn

  def close_conn_handler(self, handle: FTP) -> None:
    """Closes a connection, falling back to a raw close if the graceful QUIT fails.

    Args:
      handle: The connection to close.
    """
    try:
      handle.quit()
    except OSError:
      handle.close()


class SFTPConnector:
  __slots__ = ("_credentials",)

  def __init__(self, credentials: SFTPCredentials) -> None:
    """Stores connection credentials for later connection attempts.

    Args:
      credentials: The SFTP server credentials to connect with.
    """
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
    client.connect(
      self._credentials.host,
      port=self._credentials.port,
      username=self._credentials.username,
      password=self._credentials.password.get_secret_value() if self._credentials.password is not None else None,
      key_filename=str(self._credentials.private_key_path) if self._credentials.private_key_path is not None else None,
      passphrase=(
        self._credentials.private_key_passphrase.get_secret_value() if self._credentials.private_key_passphrase is not None else None
      ),
      timeout=self._credentials.connect_timeout,
    )
    transport = client.get_transport()
    assert transport is not None
    return transport

  def request_handler(self, transport: Transport) -> SFTPClient:
    """Opens a new SFTP channel.

    Args:
      transport: The `Transport` to open a channel on.

    Returns:
      The new channel.
    """
    return SFTPClient.from_transport(transport)  # pyright: ignore[reportReturnType]

  def close_conn_handler(self, handle: Transport) -> None:
    """Closes the whole `Transport`, and every channel opened on it.

    Args:
      handle: The `Transport` to close.
    """
    handle.close()
