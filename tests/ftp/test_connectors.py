"""Unit tests for `aeth_ext.ftp.connectors.FTPConnector`/`SFTPConnector` -- pure connection-setup
logic, no real network."""

# Standard library imports
from ftplib import FTP, FTP_TLS

# Third party imports
import pytest
from paramiko import SSHClient

# First party imports
from aeth_ext.ftp.connectors import FTPConnector, SFTPConnector
from aeth_ext.ftp.credentials import FTPCredentials, SFTPCredentials
from aeth_ext.ftp.errors import ServerNotAvailableError


def _stub_out_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
  """Replaces FTP/FTP_TLS's network-touching methods with no-ops, recording call order.
  `FTP_TLS` overrides `login` (to negotiate TLS) so it's patched separately from `FTP.login`."""
  calls: list[str] = []
  monkeypatch.setattr(FTP, "connect", lambda self, *args, **kwargs: calls.append("connect"))
  monkeypatch.setattr(FTP, "login", lambda self, *args, **kwargs: calls.append("login"))
  monkeypatch.setattr(FTP_TLS, "login", lambda self, *args, **kwargs: calls.append("login"))
  monkeypatch.setattr(FTP, "set_pasv", lambda self, val: calls.append("set_pasv"))
  monkeypatch.setattr(FTP_TLS, "prot_p", lambda self: calls.append("prot_p"))
  return calls


class TestFTPConnectorTLSDataChannel:
  def test_tls_with_default_credentials_protects_the_data_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_out_network(monkeypatch)
    connector = FTPConnector(FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", use_tls=True))  # pyright: ignore[reportArgumentType]

    connector.request_handler()

    assert calls == ["connect", "login", "prot_p", "set_pasv"]

  def test_tls_with_protect_data_channel_explicitly_true_protects_the_data_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_out_network(monkeypatch)
    connector = FTPConnector(
      FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", use_tls=True, protect_data_channel=True)  # pyright: ignore[reportArgumentType]
    )

    connector.request_handler()

    assert calls == ["connect", "login", "prot_p", "set_pasv"]

  def test_tls_with_protect_data_channel_disabled_skips_prot_p(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_out_network(monkeypatch)
    connector = FTPConnector(
      FTPCredentials(host="ftp.example.com", username="svc", password="hunter2", use_tls=True, protect_data_channel=False)  # pyright: ignore[reportArgumentType]
    )

    connector.request_handler()

    assert calls == ["connect", "login", "set_pasv"]

  def test_plain_ftp_never_calls_prot_p(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_out_network(monkeypatch)
    connector = FTPConnector(FTPCredentials(host="ftp.example.com", username="svc", password="hunter2"))  # pyright: ignore[reportArgumentType]

    connector.request_handler()

    assert calls == ["connect", "login", "set_pasv"]


class TestConnectFailureRaisesServerNotAvailable:
  """A socket-level connect() failure is the documented (README) contract for
  `ServerNotAvailableError` -- distinct from a live server rejecting credentials/a host key, which
  must keep propagating unchanged."""

  def test_ftp_connect_refused_raises_server_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(self: FTP, *args: object, **kwargs: object) -> None:
      raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(FTP, "connect", _refuse)
    connector = FTPConnector(FTPCredentials(host="ftp.example.com", username="svc", password="hunter2"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(ServerNotAvailableError) as exc_info:
      connector.request_handler()

    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)

  def test_ftp_login_rejection_is_not_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
    # connect() succeeding means the server *was* reached -- a rejected login is an entirely
    # different, non-connectivity failure and must not be reclassified as ServerNotAvailableError.
    # Standard library imports
    from ftplib import error_perm

    _stub_out_network(monkeypatch)

    def _reject(self: FTP, *args: object, **kwargs: object) -> None:
      raise error_perm("530 Login incorrect")

    monkeypatch.setattr(FTP, "login", _reject)
    connector = FTPConnector(FTPCredentials(host="ftp.example.com", username="svc", password="wrong"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(error_perm):
      connector.request_handler()

  def test_sftp_connect_refused_raises_server_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(self: SSHClient, *args: object, **kwargs: object) -> None:
      raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(SSHClient, "connect", _refuse)
    connector = SFTPConnector(SFTPCredentials(host="sftp.example.com", username="svc", password="hunter2"))  # pyright: ignore[reportArgumentType]

    with pytest.raises(ServerNotAvailableError) as exc_info:
      connector.get_transport()

    assert isinstance(exc_info.value.__cause__, ConnectionRefusedError)

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
