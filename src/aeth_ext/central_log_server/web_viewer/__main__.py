# Standard library imports
from logging import getLogger

# Third party imports
from rich.console import Console

# First party imports
from aeth_ext import initialize
from aeth_ext.central_log_server.web_viewer import LogWebViewApp

# SKIP_ENTRYPOINT_MARKER = True
# PROJECT_NAME is fixed; AETH_WEB_SESSION_ID (UUID only) drives per-session
# log FILE names inside the shared directory via LoggingConfig.
PROJECT_NAME = "aeth_ext.central-log-web-viewer"
# Redirect the shared Rich console to stderr so that any diagnostic output
# emitted during initialize() (e.g. the socket-probe log messages written by
# ephemeral_log_to_console) does not pollute stdout.  textual-serve captures
# stdout for its binary WebDriver protocol and will misinterpret any plain-text
# output appearing there.  The __init_logging_base machinery picks this
# constant up automatically and patches get_console() in-place.
RICH_CONSOLE = Console(stderr=True)

logger = getLogger(__name__)

if __name__ == "__main__":
  try:
    initialize(logging="socket")
  except RuntimeError as exc:
    if "Failed to connect to log server" not in str(exc):
      raise
    initialize(logging=True)

  # initialize() calls MonkeyPatcher.apply_monkey_patches() which discovers
  # WebViewerPatches in patches.py and applies the Textual logger redirect.
  logger.info("Starting %s", PROJECT_NAME)

  LogWebViewApp().run()
