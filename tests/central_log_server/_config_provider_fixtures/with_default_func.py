# Standard library imports
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.central_log_server.client.config_provider import LoggingConfigResult


def get_logging_configs() -> LoggingConfigResult:
  return {
    "local": {"source": "with_default_func"},
    "remote": {"source": "with_default_func"},
    "program_name": "default-func-program",
    "testing": True,
  }
