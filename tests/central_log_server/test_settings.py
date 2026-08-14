"""Tests for `aeth_ext.central_log_server.settings.Settings`."""

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.settings import Settings

_DEFAULT_STATE_PUSH_PORT = 9021

_ENV_VAR_NAMES = [
  "PERSISTED_DIR_LOC",
  "DEBUG_WAIT_FOR_CLIENT",
  "WEB_VIEWER_PUBLIC_URL",
  "COOLIFY_URL",
  "WEB_VIEWER_SERVE_HOST",
  "WEB_VIEWER_SERVE_PORT",
  "STATE_PUSH_HOST",
  "STATE_PUSH_PORT",
  "ALERTS_EMAIL_PWD",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
  for name in _ENV_VAR_NAMES:
    monkeypatch.delenv(name, raising=False)


def _make_settings(**overrides: object) -> Settings:
  overrides.setdefault("ALERTS_EMAIL_PWD", "test-password")
  return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


class TestDefaults:
  def test_debug_wait_for_client_defaults_false(self) -> None:
    assert _make_settings().debug_wait_for_client is False

  def test_web_viewer_public_url_defaults_none(self) -> None:
    assert _make_settings().web_viewer_public_url is None

  def test_state_push_defaults(self) -> None:
    settings = _make_settings()

    assert settings.state_push_host == "127.0.0.1"
    assert settings.state_push_port == _DEFAULT_STATE_PUSH_PORT


class TestStripTrailingSlashValidator:
  def test_trailing_slash_is_stripped(self) -> None:
    settings = _make_settings(WEB_VIEWER_PUBLIC_URL="https://example.test/")

    assert settings.web_viewer_public_url == "https://example.test"

  def test_multiple_trailing_slashes_are_stripped(self) -> None:
    settings = _make_settings(WEB_VIEWER_PUBLIC_URL="https://example.test///")

    assert settings.web_viewer_public_url == "https://example.test"

  def test_no_trailing_slash_is_unchanged(self) -> None:
    settings = _make_settings(WEB_VIEWER_PUBLIC_URL="https://example.test")

    assert settings.web_viewer_public_url == "https://example.test"

  def test_none_stays_none(self) -> None:
    settings = _make_settings(WEB_VIEWER_PUBLIC_URL=None)

    assert settings.web_viewer_public_url is None


class TestWebViewerPublicUrlAliasChoices:
  def test_falls_back_to_coolify_url_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLIFY_URL", "https://coolify.example.test/")

    settings = _make_settings()

    assert settings.web_viewer_public_url == "https://coolify.example.test"

  def test_primary_alias_takes_precedence_over_coolify_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOLIFY_URL", "https://coolify.example.test")
    monkeypatch.setenv("WEB_VIEWER_PUBLIC_URL", "https://primary.example.test")

    settings = _make_settings()

    assert settings.web_viewer_public_url == "https://primary.example.test"
