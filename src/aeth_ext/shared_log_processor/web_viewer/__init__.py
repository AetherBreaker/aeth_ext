# Standard library imports
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
from textual.app import App

# First party imports
from aeth_ext.command_client.client import CommandClient
from aeth_ext.shared_log_processor.settings import Settings
from aeth_ext.shared_log_processor.web_viewer.screens.log_stream import LogStreamScreen
from aeth_ext.shared_log_processor.web_viewer.screens.program_selection import ProgramSelectionScreen

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.shared_log_processor.web_viewer.screens.log_picker import FileChosen

CWD = Path.cwd()


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
    self.command_client = CommandClient()

  def on_mount(self) -> None:
    self.push_screen(ProgramSelectionScreen(self._log_root, self.command_client))

  def on_file_chosen(self, event: FileChosen) -> None:
    self.push_screen(LogStreamScreen(event.path))

  async def on_unmount(self) -> None:
    await self.command_client.close()
