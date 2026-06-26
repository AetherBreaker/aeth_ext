# Standard library imports
import sys
from email.headerregistry import Address
from logging import getLogger
from os import environ
from pathlib import Path
from typing import Annotated, Self
from zoneinfo import ZoneInfo

# Third party imports
from pydantic import Field
from pydantic_settings import BaseSettings as _BaseSettings, SettingsConfigDict

# First party imports
from aeth_ext.types.abc import CapturesSubclasses

logger = getLogger(__name__)

environ.setdefault("PYDANTIC_ERRORS_INCLUDE_URL", "false")


CWD = Path(__file__).parent if getattr(sys, "frozen", False) else Path.cwd()

__all__ = ["BaseSettings"]

type AddressLike = str | Address | tuple[str, str | None, str | None, str | None]


class BaseSettings(_BaseSettings, CapturesSubclasses):
  model_config = (
    SettingsConfigDict(
      env_file=CWD / ".env",
      env_file_encoding="utf-8",
      env_ignore_empty=True,
      extra="ignore",
    )
    if __debug__
    else SettingsConfigDict()
  )

  persisted_dir_loc: Annotated[Path, Field(alias="PERSISTED_DIR_LOC")] = (
    CWD / "persisted_data" if __debug__ else Path("/app/persisted_data")
  )

  alerts_smtp_server: Annotated[str, Field(alias="ALERTS_SMTP_SERVER")] = "smtppro.zoho.com"
  alerts_smtp_port: Annotated[int, Field(alias="ALERTS_SMTP_PORT")] = 587
  alerts_email: Annotated[AddressLike, Field(alias="ALERTS_EMAIL")] = "info@sweetfiretobacco.com"
  alerts_email_pwd: Annotated[str, Field(alias="ALERTS_EMAIL_PWD")]
  alerts_recipients: Annotated[frozenset[AddressLike], Field(alias="ALERTS_RECIPIENTS")] = frozenset(
    {"jacob.ogden@sweetfiretobacco.com"}
  )

  log_loc_folder: Annotated[Path, Field(alias="LOG_LOC_FOLDER")] = persisted_dir_loc / "logs"

  tz: Annotated[ZoneInfo, Field(alias="TZ")] = ZoneInfo("US/Eastern")

  def creds_file_reusable(self, err_msg: str, *expected_path_parts: str) -> Path:
    fp = self.persisted_dir_loc.joinpath(*expected_path_parts)
    if not fp.exists() or not fp.is_file():
      raise FileNotFoundError(f"{err_msg}: {fp}")
    return fp

  # Make this an alias of get_final_model to maintain compatibility with existing code that uses get_settings
  @classmethod
  def get_settings(cls) -> Self:
    return cls.get_final_model()  # pyright: ignore[reportReturnType]


if __name__ == "__main__":
  settings = BaseSettings.get_settings()
  pass
