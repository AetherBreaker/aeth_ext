# Standard library imports
import sys
from logging import getLogger
from pathlib import Path
from typing import Annotated

# Third party imports
from pydantic import AfterValidator, Field
from pydantic.aliases import AliasChoices

# First party imports
from aeth_ext.settings import BaseSettings

logger = getLogger(__name__)


CWD = Path(__file__).parent if getattr(sys, "frozen", False) else Path.cwd()


def strip_trailing_slash(v: str | None) -> str | None:
  return v.rstrip("/") if v is not None else v


class Settings(BaseSettings):
  persisted_dir_loc: Annotated[Path, Field(alias="PERSISTED_DIR_LOC")] = (
    CWD / "persisted_data" if __debug__ else Path("/app/persisted_data")
  )

  debug_wait_for_client: bool = False

  web_viewer_public_url: Annotated[str | None, AfterValidator(strip_trailing_slash), Field(alias="WEB_VIEWER_PUBLIC_URL")] = Field(
    default=None,
    alias="WEB_VIEWER_PUBLIC_URL",
    validation_alias=AliasChoices("WEB_VIEWER_PUBLIC_URL", "COOLIFY_URL"),
  )

  web_viewer_serve_host: Annotated[str, Field(alias="WEB_VIEWER_SERVE_HOST")] = "localhost"
  web_viewer_serve_port: Annotated[int, Field(alias="WEB_VIEWER_SERVE_PORT")] = 8080

  state_query_host: Annotated[str, Field(alias="STATE_QUERY_HOST")] = "127.0.0.1"
  state_query_port: Annotated[int, Field(alias="STATE_QUERY_PORT")] = 9021
