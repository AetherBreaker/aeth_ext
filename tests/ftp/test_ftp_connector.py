# Standard library imports
from ftplib import FTP, FTP_TLS, error_temp

# Third party imports
import pytest

# First party imports
from aeth_ext.ftp.credentials import FTPCredentials
from aeth_ext.ftp.errors import ServerCapacityError, ServerNotAvailableError
from aeth_ext.ftp.ftp_connector import FTPConnector


def _stub_out_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
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


class TestCapacityRefusalClassification:
  @staticmethod
  def _refuse(monkeypatch: pytest.MonkeyPatch, reply: str) -> BaseException:
    monkeypatch.setattr(FTP, "connect", lambda self, *a, **k: None)

    def _login(self: FTP, *_a: object, **_k: object) -> None:
      raise error_temp(reply)

    monkeypatch.setattr(FTP, "login", _login)
    monkeypatch.setattr(FTP, "close", lambda self: None)
    connector = FTPConnector(FTPCredentials(host="ftp.example.com", username="svc", password="hunter2"))  # pyright: ignore[reportArgumentType]
    try:
      connector.request_handler()
    except BaseException as exc:  # noqa: BLE001 -- the classification under test is exactly which type comes back
      return exc
    raise AssertionError("request_handler did not raise")

  @pytest.mark.parametrize(
    "reply",
    [
      pytest.param("421 There are too many connected users, please try later.", id="vsftpd-verified-live"),
      pytest.param("421 Too many users are connected, please try again later.", id="filezilla-server"),
      pytest.param("421 Too many users are connected.", id="iis"),
      pytest.param("421 Sorry, the maximum number of clients (5) for this server has been reached.", id="proftpd"),
      pytest.param("421 Sorry, the maximum number of clients (10) from your host are already connected.", id="pure-ftpd"),
      pytest.param("421 Server reached its connection limit.", id="generic-connection-limit"),
    ],
  )
  def test_a_real_daemon_capacity_refusal_is_classified(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    assert isinstance(self._refuse(monkeypatch, reply), ServerCapacityError)

  @pytest.mark.parametrize(
    "reply",
    [
      pytest.param("421 Service not available, remote server has closed connection.", id="generic-unavailable"),
      pytest.param("421 Cannot accept your connection right now.", id="novel-wording"),
      pytest.param("421 \u5f53\u524d\u8fde\u63a5\u6570\u5df2\u8fbe\u4e0a\u9650\u3002", id="non-english"),
    ],
  )
  def test_an_unrecognised_421_is_treated_as_capacity(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    # The point of the inversion. None of these name a wording anyone anticipated -- and that is
    # exactly the case that used to disable ceiling discovery outright. Capping the pool until the
    # next re-probe is the recoverable outcome; never capping it is not.
    assert isinstance(self._refuse(monkeypatch, reply), ServerCapacityError)

  @pytest.mark.parametrize(
    "reply",
    [
      pytest.param("421 Server is going down for maintenance.", id="maintenance"),
      pytest.param("421 Server shutting down, please try again later.", id="proftpd-shutdown"),
      pytest.param("421 Service temporarily unavailable, restarting.", id="restarting"),
    ],
  )
  def test_a_planned_outage_421_stays_a_plain_transient(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    # A server that says why it is unavailable should have that reason reach the caller, rather than
    # the pool swallowing it into a blocking wait that eventually reads as "pool saturated".
    exc = self._refuse(monkeypatch, reply)
    assert isinstance(exc, error_temp)
    assert not isinstance(exc, ServerCapacityError)

  def test_a_non_421_reply_is_never_a_capacity_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
    # The marker check is only consulted for a 421; the code path must not fire on other codes even
    # when the wording happens to match.
    exc = self._refuse(monkeypatch, "450 Too many open files on the server.")
    assert not isinstance(exc, ServerCapacityError)
