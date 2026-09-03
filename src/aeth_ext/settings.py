"""Base ``pydantic-settings`` model for alerting, logging, and timezone configuration."""

# Standard library imports
import sys
from email.headerregistry import Address
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from os import environ
from pathlib import Path
from typing import Annotated, Self
from zoneinfo import ZoneInfo

# Third party imports
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings as _BaseSettings, SettingsConfigDict

# First party imports
from aeth_ext.static_eval import get_caller_file
from aeth_ext.types.subclass_capture import CapturesSubclasses

logger = getLogger(__name__)

environ.setdefault("PYDANTIC_ERRORS_INCLUDE_URL", "false")


CWD = Path(__file__).parent if getattr(sys, "frozen", False) else Path.cwd()

__all__ = ["BaseSettings"]

type AddressLike = str | Address | tuple[str, str | None, str | None, str | None]


class BaseSettings(_BaseSettings, CapturesSubclasses):
  """Project settings loaded from environment variables, plus the CWD's ``.env`` under ``__debug__``.

  Combines ``pydantic_settings.BaseSettings`` with ``CapturesSubclasses`` so ``get_settings`` can
  return the most locally-defined subclass without the caller naming it.
  """

  model_config = (
    SettingsConfigDict(
      env_file=CWD / ".env",
      env_file_encoding="utf-8",
      env_ignore_empty=True,
      extra="ignore",
    )
    if __debug__
    else SettingsConfigDict(
      env_ignore_empty=True,
      extra="ignore",
    )
  )

  persisted_dir_loc: Annotated[Path, Field(alias="PERSISTED_DIR_LOC")] = (
    CWD / "persisted_data" if __debug__ else Path("/app/persisted_data")
  )

  alerts_smtp_server: Annotated[str, Field(alias="ALERTS_SMTP_SERVER")] = "smtppro.zoho.com"
  alerts_smtp_port: Annotated[int, Field(alias="ALERTS_SMTP_PORT")] = 587
  alerts_email: Annotated[AddressLike, Field(alias="ALERTS_EMAIL")] = "info@sweetfiretobacco.com"
  alerts_email_pwd: Annotated[SecretStr, Field(alias="ALERTS_EMAIL_PWD")]
  alerts_recipients: Annotated[frozenset[AddressLike], Field(alias="ALERTS_RECIPIENTS")] = frozenset(
    {"jacob.ogden@sweetfiretobacco.com"}
  )

  # Pushover (https://pushover.net) push-notification alerting. Independent of
  # the SMTP alerting above -- separate auth (API token, not an email login)
  # and separate delivery infra, so an outage or lockout of one channel can't
  # take out the other. Both must be set for send_alert_push to actually send.
  alerts_pushover_token: Annotated[SecretStr | None, Field(alias="ALERTS_PUSHOVER_TOKEN")] = None
  alerts_pushover_user_key: Annotated[SecretStr | None, Field(alias="ALERTS_PUSHOVER_USER_KEY")] = None

  # Dead-man's-switch ping URL (e.g. a healthchecks.io check's ping URL). When
  # set, periodic heartbeats also ping this URL so the external service can
  # alert on a stale/missing heartbeat -- catching a hung process, not just a
  # crashed one.
  alerts_healthcheck_ping_url: Annotated[SecretStr | None, Field(alias="ALERTS_HEALTHCHECK_PING_URL")] = None
  alerts_healthcheck_pingkey: Annotated[SecretStr | None, Field(alias="PINGKEY")] = None

  log_conn_host: Annotated[str, Field(alias="LOG_CONN_HOST")] = "central-log-server" if sys.platform != "win32" else "localhost"
  log_conn_port: Annotated[int, Field(alias="LOG_CONN_PORT")] = DEFAULT_TCP_LOGGING_PORT

  log_loc_folder: Annotated[Path, Field(alias="LOG_LOC_FOLDER", default_factory=lambda data: data["persisted_dir_loc"] / "logs")]

  logging_config_loc: Annotated[Path | None, Field(alias="LOGGING_CONFIG_LOC")] = None

  # Client log-history housekeeping (`aeth_ext.central_log_server.client.history`): history files
  # older than this many days are deleted, and a low-priority alert fires once when a program's
  # history directory grows past this many bytes (nothing is deleted on that path).
  log_history_retention_days: Annotated[int, Field(alias="LOG_HISTORY_RETENTION_DAYS", ge=1)] = 7
  log_history_max_bytes: Annotated[int, Field(alias="LOG_HISTORY_MAX_BYTES", ge=0)] = 1024**3

  # `CustomTimedRotatingFileHandler` raises a low-priority alert when the file it is writing
  # reaches this size, and again at every doubling, so a runaway logger is noticed while it is
  # happening. 0 disables. Overridable per handler via its `size_warn_bytes` config key.
  log_file_size_warn_bytes: Annotated[int, Field(alias="LOG_FILE_SIZE_WARN_BYTES", ge=0)] = 256 * 1024**2

  # Whether the logging DictConfigurator may unpickle base64 cloudpickle
  # "definition" entries in a config. Disable on deployments that must never
  # execute pickled payloads (e.g. a log server exposed beyond trusted hosts).
  logging_allow_pickled_definitions: Annotated[bool, Field(alias="LOGGING_ALLOW_PICKLED_DEFINITIONS")] = True

  tz: Annotated[ZoneInfo, Field(alias="TZ")] = ZoneInfo("US/Eastern")

  def _creds_file_reusable(self, err_msg: str, *expected_path_parts: str) -> Path:
    fp = self.persisted_dir_loc.joinpath(*expected_path_parts)
    if not fp.exists() or not fp.is_file():
      raise FileNotFoundError(f"{err_msg}: {fp}")
    return fp

  # Make this an alias of get_final_model to maintain compatibility with existing code that uses get_settings
  @classmethod
  def get_settings(cls, caller_file: str | None = None) -> Self:
    """Return the most local subclass's settings instance; alias of ``get_final_model`` kept for existing callers."""
    if caller_file is None:
      caller_file = get_caller_file(1)
    return cls.get_final_model(caller_file=caller_file)  # pyright: ignore[reportReturnType]


if __name__ == "__main__":
  settings = BaseSettings.get_settings()
