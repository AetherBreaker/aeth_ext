# Standard library imports
import sys
from logging import getLogger
from pathlib import Path
from typing import Annotated

# Third party imports
from pydantic import Field

# First party imports
from aeth_ext.settings import BaseSettings

logger = getLogger(__name__)


CWD = Path(__file__).parent if getattr(sys, "frozen", False) else Path.cwd()


class Settings(BaseSettings):
  persisted_dir_loc: Annotated[Path, Field(alias="PERSISTED_DIR_LOC")] = (
    CWD / "persisted_data" if __debug__ else Path("/app/persisted_data")
  )

  debug_wait_for_client: bool = False

  file_serve_public_url: Annotated[str | None, Field(alias="FILE_SERVE_PUBLIC_URL")] = (
    "https://r68g367p9a5jnsic9kwx8dti.108.61.75.238.sslip.io"
  )
  file_serve_host: Annotated[str, Field(alias="FILE_SERVE_HOST")] = "localhost"
  file_serve_port: Annotated[int, Field(alias="FILE_SERVE_PORT")] = 8080
