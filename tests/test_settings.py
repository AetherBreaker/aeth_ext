"""Tests for `aeth_ext.settings.BaseSettings`.

These construct `BaseSettings` directly with ``_env_file=None`` and never rely
on this repo's real ``.env`` (which holds live credentials) -- see
`aeth_ext.settings.CWD` / `SettingsConfigDict(env_file=...)`. Any relevant env
vars are also cleared so tests are hermetic regardless of the host's actual
environment.
"""

# Standard library imports
from email.headerregistry import Address
from typing import TYPE_CHECKING

# Third party imports
import pytest
from hypothesis import given, strategies as st

# First party imports
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_ENV_VAR_NAMES = [
  "PERSISTED_DIR_LOC",
  "ALERTS_SMTP_SERVER",
  "ALERTS_SMTP_PORT",
  "ALERTS_EMAIL",
  "ALERTS_EMAIL_PWD",
  "ALERTS_RECIPIENTS",
  "LOG_CONN_HOST",
  "LOG_CONN_PORT",
  "LOG_LOC_FOLDER",
  "LOGGING_CONFIG_LOC",
  "LOGGING_ALLOW_PICKLED_DEFINITIONS",
  "TZ",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
  """Never let host/CI environment variables or the repo's real .env leak in."""
  for name in _ENV_VAR_NAMES:
    monkeypatch.delenv(name, raising=False)


def _make_settings(**overrides: object) -> BaseSettings:
  overrides.setdefault("ALERTS_EMAIL_PWD", "test-password")
  return BaseSettings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


class TestFieldDefaults:
  def test_alerts_email_pwd_is_required(self):
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
      BaseSettings(_env_file=None)  # pyright: ignore[reportCallIssue]

  def test_default_alerts_recipients_is_a_frozenset(self):
    settings = _make_settings()

    assert isinstance(settings.alerts_recipients, frozenset)

  def test_log_loc_folder_defaults_under_persisted_dir_loc(self):
    settings = _make_settings()

    assert settings.log_loc_folder == settings.persisted_dir_loc / "logs"

  def test_log_loc_folder_tracks_an_overridden_persisted_dir_loc(self, tmp_path: Path):
    settings = _make_settings(PERSISTED_DIR_LOC=tmp_path)

    assert settings.persisted_dir_loc == tmp_path
    assert settings.log_loc_folder == tmp_path / "logs"

  def test_explicit_log_loc_folder_overrides_the_derived_default(self, tmp_path: Path):
    explicit = tmp_path / "somewhere-else"
    settings = _make_settings(PERSISTED_DIR_LOC=tmp_path, LOG_LOC_FOLDER=explicit)

    assert settings.log_loc_folder == explicit


class TestEnvVarOverride:
  def test_env_var_overrides_a_default(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALERTS_SMTP_SERVER", "smtp.example.test")

    settings = _make_settings()

    assert settings.alerts_smtp_server == "smtp.example.test"

  def test_kwarg_takes_precedence_over_env_var(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALERTS_SMTP_SERVER", "from-env.example.test")

    settings = _make_settings(ALERTS_SMTP_SERVER="from-kwarg.example.test")

    assert settings.alerts_smtp_server == "from-kwarg.example.test"


class TestAddressLikeCoercion:
  @given(st.emails())
  def test_plain_string_address_is_accepted_as_is(self, email: str):
    settings = _make_settings(ALERTS_EMAIL=email)

    assert settings.alerts_email == email

  @given(
    display_name=st.text(min_size=1, max_size=20).filter(lambda s: "\x00" not in s),
    username=st.from_regex(r"[a-zA-Z0-9_]{1,10}", fullmatch=True),
    domain=st.from_regex(r"[a-zA-Z0-9]{1,10}\.[a-z]{2,5}", fullmatch=True),
  )
  def test_four_tuple_address_round_trips_unchanged(self, display_name: str, username: str, domain: str):
    """The packed-tuple `AddressLike` branch is stored as-is by pydantic; the
    actual conversion into an `Address` object happens later, via
    `aeth_ext.utils.handle_addrlike`, not automatically during validation."""
    addr_tuple = (display_name, username, None, f"{username}@{domain}")

    settings = _make_settings(ALERTS_EMAIL=addr_tuple)

    assert settings.alerts_email == addr_tuple

  def test_an_actual_address_object_is_accepted_as_is(self):
    addr = Address(display_name="Test", username="test", domain="example.test")

    settings = _make_settings(ALERTS_EMAIL=addr)

    assert settings.alerts_email == addr

  @given(st.frozensets(st.emails(), min_size=0, max_size=5))
  def test_recipients_frozenset_round_trips(self, recipients: frozenset[str]):
    settings = _make_settings(ALERTS_RECIPIENTS=recipients)

    assert set(settings.alerts_recipients) == set(recipients)


class TestCredsFileReusable:
  def test_raises_when_file_missing(self, tmp_path: Path):
    settings = _make_settings(PERSISTED_DIR_LOC=tmp_path)

    with pytest.raises(FileNotFoundError):
      settings._creds_file_reusable("missing creds", "nope.json")  # pyright: ignore[reportPrivateUsage]

  def test_raises_when_path_is_a_directory(self, tmp_path: Path):
    (tmp_path / "a_directory").mkdir()
    settings = _make_settings(PERSISTED_DIR_LOC=tmp_path)

    with pytest.raises(FileNotFoundError):
      settings._creds_file_reusable("is a dir", "a_directory")  # pyright: ignore[reportPrivateUsage]

  def test_returns_path_when_file_exists(self, tmp_path: Path):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    creds_file = creds_dir / "token.json"
    creds_file.write_text("{}")
    settings = _make_settings(PERSISTED_DIR_LOC=tmp_path)

    result = settings._creds_file_reusable("should exist", "creds", "token.json")  # pyright: ignore[reportPrivateUsage]

    assert result == creds_file


class TestGetSettings:
  def test_get_settings_returns_a_base_settings_instance(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALERTS_EMAIL_PWD", "test-password")

    settings = BaseSettings.get_settings(caller_file=__file__)

    assert isinstance(settings, BaseSettings)
