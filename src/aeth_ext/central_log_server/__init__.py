"""Central log server: a TCP receiver that files client programs' log records into per-program logging hierarchies."""

# Standard library imports
from sys import platform

# Third party imports
from rich.console import Console

RICH_CONSOLE = Console(
  width=None if platform == "win32" else 165,
  log_time=platform == "win32",
)
PROJECT_NAME = "aeth_ext.central-log-server"
HEARTBEAT_SLUG = "central-log-server"
