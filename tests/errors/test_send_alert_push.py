# Standard library imports
from base64 import b64encode
from typing import TYPE_CHECKING, Self
from urllib.error import URLError
from urllib.parse import parse_qs

# Third party imports
import pytest
from pydantic import SecretStr

# First party imports
from aeth_ext.errors import send_alert_push as sap_mod

if TYPE_CHECKING:
  # Standard library imports
  from urllib.request import Request


class TestNotConfigured:
  def test_warns_and_returns_without_sending_when_token_missing(
    self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_token", None)
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_user_key", SecretStr("user-key"))
    calls: list[object] = []
    monkeypatch.setattr(sap_mod, "urlopen", lambda *a, **k: calls.append((a, k)))

    with caplog.at_level("WARNING"):
      sap_mod.send_alert_push("title", "message")

    assert calls == []
    assert any("Skipping push alert" in record.message for record in caplog.records)

  def test_warns_and_returns_without_sending_when_user_key_missing(
    self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_token", SecretStr("token"))
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_user_key", None)
    calls: list[object] = []
    monkeypatch.setattr(sap_mod, "urlopen", lambda *a, **k: calls.append((a, k)))

    with caplog.at_level("WARNING"):
      sap_mod.send_alert_push("title", "message")

    assert calls == []
    assert any("Skipping push alert" in record.message for record in caplog.records)


def _decode_body(request: Request) -> str:
  assert isinstance(request.data, bytes)
  return request.data.decode("utf-8")


class _FakeResponse:
  def __init__(self, status: int) -> None:
    self.status = status

  def __enter__(self) -> Self:
    return self

  def __exit__(self, *exc_info: object) -> None:
    pass


class TestConfigured:
  @pytest.fixture(autouse=True)
  def _configure(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_token", SecretStr("test-token"))
    monkeypatch.setattr(sap_mod.SETTINGS, "alerts_pushover_user_key", SecretStr("test-user-key"))

  def test_posts_the_title_and_message_to_the_pushover_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
      requests.append(request)
      return _FakeResponse(200)

    monkeypatch.setattr(sap_mod, "urlopen", fake_urlopen)

    sap_mod.send_alert_push("Something broke", "the traceback")

    (request,) = requests
    assert request.full_url == sap_mod._PUSHOVER_API_URL  # pyright: ignore[reportPrivateUsage]
    body = _decode_body(request)
    assert "title=Something+broke" in body
    assert "message=the+traceback" in body
    assert "token=test-token" in body
    assert "user=test-user-key" in body

  def test_emergency_priority_adds_retry_and_expire_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
      requests.append(request)
      return _FakeResponse(200)

    monkeypatch.setattr(sap_mod, "urlopen", fake_urlopen)

    sap_mod.send_alert_push("title", "message", priority=2)

    (request,) = requests
    body = _decode_body(request)
    assert "priority=2" in body
    assert "retry=" in body
    assert "expire=" in body

  def test_non_200_status_is_logged_not_raised(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(sap_mod, "urlopen", lambda request, timeout: _FakeResponse(500))

    with caplog.at_level("ERROR"):
      sap_mod.send_alert_push("title", "message")  # must not raise

    assert any("Pushover alert failed" in record.message for record in caplog.records)

  def test_url_error_is_caught_and_logged_not_propagated(
    self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    def raising_urlopen(request: Request, timeout: float) -> _FakeResponse:
      raise URLError("network is down")

    monkeypatch.setattr(sap_mod, "urlopen", raising_urlopen)

    with caplog.at_level("ERROR"):
      sap_mod.send_alert_push("title", "message")  # must not raise

    assert any("Failed to send push alert" in record.message for record in caplog.records)

  def test_no_attachment_fields_when_image_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
      requests.append(request)
      return _FakeResponse(200)

    monkeypatch.setattr(sap_mod, "urlopen", fake_urlopen)

    sap_mod.send_alert_push("title", "message")

    (request,) = requests
    body = _decode_body(request)
    assert "attachment_base64" not in body
    assert "attachment_type" not in body

  def test_image_is_base64_encoded_and_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
      requests.append(request)
      return _FakeResponse(200)

    monkeypatch.setattr(sap_mod, "urlopen", fake_urlopen)

    image_bytes = b"\x89PNG-fake-image-bytes"
    sap_mod.send_alert_push("title", "message", image=image_bytes)

    (request,) = requests
    fields = parse_qs(_decode_body(request))
    assert fields["attachment_base64"][0] == b64encode(image_bytes).decode("ascii")
    assert fields["attachment_type"][0] == "image/png"

  def test_message_under_the_length_limit_is_sent_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    monkeypatch.setattr(sap_mod, "urlopen", lambda request, timeout: (requests.append(request), _FakeResponse(200))[1])

    sap_mod.send_alert_push("title", "a short message")

    fields = parse_qs(_decode_body(requests[0]))
    assert fields["message"][0] == "a short message"

  def test_message_over_the_length_limit_is_truncated_with_a_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    monkeypatch.setattr(sap_mod, "urlopen", lambda request, timeout: (requests.append(request), _FakeResponse(200))[1])

    long_message = "x" * 2000
    sap_mod.send_alert_push("title", long_message)

    sent_message = parse_qs(_decode_body(requests[0]))["message"][0]
    assert len(sent_message) == sap_mod._MESSAGE_MAX_LEN  # pyright: ignore[reportPrivateUsage]
    assert sent_message.endswith("for the full traceback)")
