# Standard library imports
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.central_log_server.client.config_provider import LoggingConfigResult


def get_logging_configs() -> LoggingConfigResult:
  time.sleep(999)
  raise AssertionError("unreachable")
