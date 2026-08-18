"""Unit tests for `aeth_ext.ftp.connectors.FTPConnector` -- pure connection-setup logic, no real network."""

# Standard library imports
from ftplib import FTP, FTP_TLS
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.ftp.connectors import FTPConnector
from aeth_ext.ftp.credentials import FTPCredentials

if TYPE_CHECKING:
  # Third party imports
  import pytest


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
