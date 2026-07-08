# Standard library imports
import time
from logging import getLogger
from pathlib import Path

# First party imports
from aeth_ext.shared_log_processor.settings import Settings

logger = getLogger(__name__)

__all__ = ["WAKE_TOKEN_PATH", "read_wake_token", "touch_wake_token"]

settings = Settings.get_settings()

# Cross-process signal file. The main log-server process (which owns the
# InLoopServer HTTP endpoint) bumps this token whenever a command server pings
# the wake endpoint; each web-viewer app subprocess watches it and re-runs
# command-server discovery promptly instead of waiting for its periodic sweep.
WAKE_TOKEN_PATH: Path = settings.persisted_dir_loc / "shared_log_processor" / "command_wake.token"


def touch_wake_token() -> None:
  """Bump the wake token so watching web-viewer sessions refresh command servers."""
  try:
    WAKE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    WAKE_TOKEN_PATH.write_text(str(time.time_ns()))
  except OSError:
    logger.warning("Failed to write command wake token", exc_info=True)


def read_wake_token() -> str | None:
  """Return the current wake token, or ``None`` if it has never been written."""
  try:
    return WAKE_TOKEN_PATH.read_text()
  except OSError:
    return None
