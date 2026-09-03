"""Fatal-exception reporting, out-of-band alerting, and shutdown driving."""

# Local folder imports
from .err_handling import (
  alert,
  alert_exception,
  handle_fatal_exc_async,
  handle_fatal_exc_sync,
  report_exc,
  trigger_shutdown,
)

__all__ = [
  "alert",
  "alert_exception",
  "handle_fatal_exc_async",
  "handle_fatal_exc_sync",
  "report_exc",
  "trigger_shutdown",
]
