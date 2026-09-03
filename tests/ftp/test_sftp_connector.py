"""Unit tests for `aeth_ext.ftp.sftp_connector.SFTPConnector` -- pure connection-setup logic, no
real network.
"""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest
from paramiko import SSHClient

# First party imports
from aeth_ext.ftp.credentials import SFTPCredentials
from aeth_ext.ftp.errors import ServerNotAvailableError
from aeth_ext.ftp.sftp_connector import SFTPConnector

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


class TestConnectFailureRaisesServerNotAvailable:
  """A socket-level connect() failure is the documented (README) contract for
  `ServerNotAvailableError` -- distinct from a live server rejecting credentials/a host key, which
  must keep propagating unchanged.
  """

  def test_sftp_connect_refused_raises_server_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(self: SSHClient, *args: object, **kwargs: object) -> None:
      raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(SSHClient, "connect", _refuse)
    connector = SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(ServerNotAvailableError) as exc_info:
      connector.get_transport()

    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)

  def test_sftp_missing_private_key_file_raises_file_not_found_not_server_not_available(self, tmp_path: Path) -> None:
    # Paramiko only discovers a missing/unreadable key file deep inside connect()'s auth phase, well
    # after the socket already succeeded, inside a handler that doesn't catch OSError -- so it must
    # never reach SSHClient.connect() at all for this to be classified correctly as a config error
    # rather than "server unreachable." Checked once at construction (SFTPConnector is built once per
    # adapter and reused for its whole lifetime), not on every get_transport() call.
    with pytest.raises(FileNotFoundError):
      SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", private_key_path=tmp_path / "does-not-exist.pem"))

  def test_sftp_private_key_path_is_a_directory_raises_immediately(self, tmp_path: Path) -> None:
    # A stat()-only preflight would let a directory pass (it exists), then paramiko would raise an
    # OSError deep inside connect(), getting misclassified as ServerNotAvailableError. Actually
    # opening the path catches this the same way as a missing file -- the specific exception type is
    # platform-dependent (IsADirectoryError on POSIX, PermissionError on Windows), so only the
    # broader OSError contract is asserted here.
    key_dir = tmp_path / "not-a-file"
    key_dir.mkdir()

    with pytest.raises((IsADirectoryError, PermissionError)):
      SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", private_key_path=key_dir))

  def test_sftp_connect_refused_with_valid_key_file_still_raises_server_not_available(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """The OSError handler's re-check of private_key_path must not misclassify a genuine network
    failure as a key-file problem when the key file is still perfectly readable.
    """
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("not a real key, never parsed -- connect() is mocked")

    def _refuse(self: SSHClient, *args: object, **kwargs: object) -> None:
      raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(SSHClient, "connect", _refuse)
    connector = SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", private_key_path=key_file))

    with pytest.raises(ServerNotAvailableError) as exc_info:
      connector.get_transport()

    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)

  def test_sftp_key_file_removed_after_construction_raises_file_not_found_not_server_not_available(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """__init__ only proves the key file was readable once -- this connector is reused for the
    adapter's whole lifetime, so a network-shaped OSError from connect() must still be re-checked
    against the key file's *current* state, not misclassified as ServerNotAvailableError if it
    vanished since construction (D-copilot regression).
    """
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("not a real key, never parsed -- connect() is mocked")
    connector = SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", private_key_path=key_file))
    key_file.unlink()

    def _refuse(self: SSHClient, *args: object, **kwargs: object) -> None:
      raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(SSHClient, "connect", _refuse)

    with pytest.raises(FileNotFoundError):
      connector.get_transport()

  def test_sftp_authentication_failure_is_not_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
    # connect() succeeding means the server *was* reached -- rejected credentials are an entirely
    # different, non-connectivity failure and must not be reclassified as ServerNotAvailableError.
    # Third party imports
    from paramiko import AuthenticationException

    def _reject(self: SSHClient, *args: object, **kwargs: object) -> None:
      raise AuthenticationException("authentication failed")

    monkeypatch.setattr(SSHClient, "connect", _reject)
    connector = SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", password="wrong"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(AuthenticationException):
      connector.get_transport()
