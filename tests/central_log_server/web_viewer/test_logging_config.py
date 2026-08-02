"""Tests for `aeth_ext.central_log_server.web_viewer.logging_config.LoggingConfig`."""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.web_viewer.logging_config import LoggingConfig
from aeth_ext.logging.setup import BaseLoggingConfig

if TYPE_CHECKING:
  # Standard library imports
  from typing import Any


@pytest.fixture(autouse=True)
def _fake_base_config(monkeypatch: pytest.MonkeyPatch):
  """Isolate LoggingConfig's own session-id substitution logic from the real
  TOML-loading pipeline (already covered by tests/logging/*) by controlling
  exactly what the parent's get_default_remote_config returns."""

  def fake_get_default_remote_config(cls: type[BaseLoggingConfig], logging_file_name: str) -> dict[str, Any]:
    return {
      "handlers": {
        "textual_debug": {"filename": "logdir://textual_debug.log"},
        "textual_info": {"filename": "logdir://textual.log"},
        "other_handler": {"filename": "logdir://other.log"},
      },
      "logging_file_name_seen": logging_file_name,
    }

  monkeypatch.setattr(BaseLoggingConfig, "get_default_remote_config", classmethod(fake_get_default_remote_config))


class TestSessionIdSubstitution:
  def test_uses_aeth_web_session_id_env_var_when_set(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AETH_WEB_SESSION_ID", "session-uuid-123")

    config = LoggingConfig.get_default_remote_config("fallback_name")

    assert config["logging_file_name_seen"] == "session-uuid-123"

  def test_falls_back_to_logging_file_name_when_env_var_unset(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AETH_WEB_SESSION_ID", raising=False)

    config = LoggingConfig.get_default_remote_config("fallback_name")

    assert config["logging_file_name_seen"] == "fallback_name"

  def test_rewrites_textual_debug_handler_filename(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AETH_WEB_SESSION_ID", "session-uuid-123")

    config = LoggingConfig.get_default_remote_config("fallback_name")

    assert config["handlers"]["textual_debug"]["filename"] == "logdir://session-uuid-123_textual_debug.log"

  def test_rewrites_textual_info_handler_filename(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AETH_WEB_SESSION_ID", "session-uuid-123")

    config = LoggingConfig.get_default_remote_config("fallback_name")

    assert config["handlers"]["textual_info"]["filename"] == "logdir://session-uuid-123_textual.log"

  def test_other_handlers_are_left_untouched(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AETH_WEB_SESSION_ID", "session-uuid-123")

    config = LoggingConfig.get_default_remote_config("fallback_name")

    assert config["handlers"]["other_handler"]["filename"] == "logdir://other.log"
