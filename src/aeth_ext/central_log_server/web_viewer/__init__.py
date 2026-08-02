# Standard library imports
from typing import TYPE_CHECKING, override

# Third party imports
from textual.app import App

# First party imports
from aeth_ext.central_log_server.settings import Settings
from aeth_ext.central_log_server.web_viewer.screens.log_picker import FileChosen, LogPickerScreen
from aeth_ext.central_log_server.web_viewer.screens.log_stream import LogStreamScreen
from aeth_ext.errors import alert_exception

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


settings = Settings.get_settings()
listening_for_debugger = False if settings.debug_wait_for_client else None


class LogWebViewApp(App[None]):
  """Textual app that lets a user pick and live-stream a server log file."""

  CSS = """
  Screen {
    layout: vertical;
  }

  #picker-title {
    padding: 1 2;
    text-style: bold;
    color: $accent;
  }

  #log-tree {
    height: 1fr;
    margin: 0 1 1 1;
    border: wide $panel;
    overflow-x: auto;
  }

  #stream-header {
    height: auto;
    padding: 1 2;
    background: $surface;
  }

  #stream-header .label {
    width: 11;
    color: $accent;
    text-style: bold;
  }

  #stream-path {
    width: 1fr;
    text-style: italic;
  }

  #stream-log {
    height: 1fr;
    margin: 0 1 1 1;
    border: wide $panel;
    overflow-x: scroll;
    overflow-y: auto;
  }
  """

  TITLE = "Shared Log Stream"

  def __init__(self, log_root: Path | None = None) -> None:
    try:
      global listening_for_debugger
      if not listening_for_debugger and listening_for_debugger is not None:
        # Third party imports
        import debugpy  # noqa: T100

        listening_for_debugger = True
        debugpy.connect(("127.0.0.1", 5678))
        debugpy.wait_for_client()  # noqa: T100
    except ImportError:
      pass

    super().__init__()
    self._log_root = (log_root or settings.log_loc_folder).resolve()

  def on_mount(self) -> None:
    self.push_screen(LogPickerScreen(self._log_root))

  def on_file_chosen(self, event: FileChosen) -> None:
    self.push_screen(LogStreamScreen(event.path))

  @override
  def _handle_exception(self, error: Exception) -> None:
    """Alert on every uncaught exception before Textual's default crash/exit handling runs.

    `App._handle_exception` is private, but it is the single choke point
    Textual itself funnels every unhandled exception through -- workers,
    message handlers, everything -- and it always results in the app
    exiting, so it is the natural place to hook alerting in without wrapping
    every worker body individually. Each browser session runs its own
    `LogWebViewApp` subprocess (see `web_viewer/server.py`'s
    `SessionAppService`), so a crash here only ends that one viewer's
    session, not the shared log server or other concurrent viewers -- but
    without this override it happened silently, with no alert email at all.
    """
    alert_exception("LogWebViewApp", error)
    super()._handle_exception(error)
