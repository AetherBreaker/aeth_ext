"""Tests for `aeth_ext.monitoring.ping`."""

# Standard library imports
from typing import TYPE_CHECKING, Self
from urllib.error import URLError

# Third party imports
from pydantic import SecretStr

# First party imports
from aeth_ext.monitoring import ping

if TYPE_CHECKING:
  # Third party imports
  import pytest


class _FakeContextManager:
  def __enter__(self) -> Self:
    return self

  def __exit__(self, *exc_info: object) -> None:
    pass


class TestNoUrlConfigured:
  def test_none_url_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(ping, "urlopen", lambda *a, **k: calls.append((a, k)))

    ping.ping_healthcheck(None)

    assert calls == []

  def test_empty_string_url_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(ping, "urlopen", lambda *a, **k: calls.append((a, k)))

    ping.ping_healthcheck(SecretStr(""))

    assert calls == []


class TestUrlConfigured:
  def test_pings_the_configured_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/some-uuid"))

    assert pinged_urls == ["https://hc-ping.com/some-uuid"]

  def test_failure_true_pings_the_fail_suffixed_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/some-uuid"), failure=True)

    assert pinged_urls == ["https://hc-ping.com/some-uuid/fail"]

  def test_start_true_pings_the_start_suffixed_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/some-uuid"), start=True)

    assert pinged_urls == ["https://hc-ping.com/some-uuid/start"]

  def test_start_true_takes_precedence_over_failure_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/some-uuid"), start=True, failure=True)

    assert pinged_urls == ["https://hc-ping.com/some-uuid/start"]

  def test_url_error_is_caught_and_logged_not_propagated(
    self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    def raising_urlopen(url: str, timeout: float) -> _FakeContextManager:
      raise URLError("network is down")

    monkeypatch.setattr(ping, "urlopen", raising_urlopen)

    with caplog.at_level("WARNING"):
      ping.ping_healthcheck(SecretStr("https://hc-ping.com/some-uuid"))  # must not raise

    assert any("Failed to ping healthcheck" in record.message for record in caplog.records)

  def test_url_is_never_logged_in_full(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The URL can embed a secret (e.g. a healthchecks.io ping-key unwrapped from a `SecretStr`),
    so neither the routine debug line nor the failure line may write more than scheme+host to the
    log -- a caplog assertion is the only way to catch a regression here, since nothing about the
    return value or urlopen() call itself would reveal a log-only leak."""

    def raising_urlopen(url: str, timeout: float) -> _FakeContextManager:
      raise URLError("network is down")

    monkeypatch.setattr(ping, "urlopen", raising_urlopen)

    with caplog.at_level("DEBUG"):
      ping.ping_healthcheck(SecretStr("https://hc-ping.com/super-secret-key/my-app"))

    assert not any("super-secret-key" in record.message for record in caplog.records)

  def test_autoprovision_appends_create_query_param(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/mykey/my-app"), autoprovision=True)

    assert pinged_urls == ["https://hc-ping.com/mykey/my-app?create=1"]

  def test_autoprovision_query_param_comes_after_the_start_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/mykey/my-app"), start=True, autoprovision=True)

    assert pinged_urls == ["https://hc-ping.com/mykey/my-app/start?create=1"]

  def test_autoprovision_query_param_comes_after_the_fail_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
    pinged_urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeContextManager:
      pinged_urls.append(url)
      return _FakeContextManager()

    monkeypatch.setattr(ping, "urlopen", fake_urlopen)

    ping.ping_healthcheck(SecretStr("https://hc-ping.com/mykey/my-app"), failure=True, autoprovision=True)

    assert pinged_urls == ["https://hc-ping.com/mykey/my-app/fail?create=1"]


class TestAtomicHelpers:
  def test_ping_success_sends_a_plain_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool]] = []
    monkeypatch.setattr(
      ping, "ping_healthcheck", lambda url, *, failure=False, start=False: calls.append((url, failure, start))
    )

    ping.ping_success(SecretStr("https://hc-ping.com/some-uuid"))

    assert calls == [(SecretStr("https://hc-ping.com/some-uuid"), False, False)]

  def test_ping_start_sends_a_start_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool]] = []
    monkeypatch.setattr(
      ping, "ping_healthcheck", lambda url, *, failure=False, start=False: calls.append((url, failure, start))
    )

    ping.ping_start(SecretStr("https://hc-ping.com/some-uuid"))

    assert calls == [(SecretStr("https://hc-ping.com/some-uuid"), False, True)]

  def test_ping_failure_sends_a_failure_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool]] = []
    monkeypatch.setattr(
      ping, "ping_healthcheck", lambda url, *, failure=False, start=False: calls.append((url, failure, start))
    )

    ping.ping_failure(SecretStr("https://hc-ping.com/some-uuid"))

    assert calls == [(SecretStr("https://hc-ping.com/some-uuid"), True, False)]
