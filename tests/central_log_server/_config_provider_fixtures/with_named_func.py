"""Fixture module exposing a non-default function name, for `query_logging_configs` tests."""

# Standard library imports
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.central_log_server.client.config_provider import LoggingConfigResult


def custom_config_getter() -> LoggingConfigResult:
  return {
    "local": {"source": "with_named_func"},
    "remote": {"source": "with_named_func"},
    "program_name": "named-func-program",
    "testing": False,
  }
