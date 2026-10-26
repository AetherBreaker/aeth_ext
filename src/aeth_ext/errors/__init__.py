"""Fatal-exception reporting, out-of-band alerting, and shutdown driving."""

# Local folder imports
from .crash_hooks import install_loop_exception_handler
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
  "install_loop_exception_handler",
  "report_exc",
  "trigger_shutdown",
]
