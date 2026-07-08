# Standard library imports
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Third party imports
from aiohttp import web
from aiohttp.web import FileResponse, Request
from textual_serve.server import Server

# First party imports
from aeth_ext.central_log_server.web_viewer.wake import touch_wake_token

if TYPE_CHECKING:
  # Standard library imports
  from os import PathLike


log = logging.getLogger("textual-serve")

_HTTP_PORT = 80
_HTTPS_PORT = 443


class InLoopServer(Server):
  """A server that runs in the current event loop."""

  def __init__(
    self,
    command: str,
    host: str = "localhost",
    port: int = 8000,
    title: str | None = None,
    public_url: str | None = None,
    statics_path: str | PathLike[Any] = "./static",
    templates_path: str | PathLike[Any] = "./templates",
    favicon_path: str | PathLike[Any] = "./favicon.ico",
  ):
    """Initialize the server.

    Args:
        command: Command to run the Textual self.app.
        host: Host of web application.
        port: Port for server.
        title: Title of the self.app.
        public_url: Public URL for the server.
        statics_path: Path to statics folder.
        templates_path: Path to templates folder.
        favicon_path: Path to favicon file.
    """
    super().__init__(
      command=command,
      host=host,
      port=port,
      title=title,
      public_url=public_url,
      statics_path=statics_path,
      templates_path=templates_path,
    )
    base_path = (Path(__file__) / "../").resolve().absolute()
    self.favicon_path = base_path / favicon_path

    self.runner: web.AppRunner | None = None
    self.site: web.TCPSite | None = None
    self.app: web.Application | None = None

  async def favicon(self, request: Request) -> FileResponse:
    return FileResponse(self.favicon_path)

  async def command_server_wake(self, request: Request) -> web.Response:
    """Command servers POST here on startup to prompt viewers to re-discover them."""
    touch_wake_token()
    return web.Response(status=204)

  async def serve_in_loop(self, debug: bool = False) -> web.AppRunner:
    """Serve the Textual application in an already-running event loop.

    Args:
        debug: Enable debug mode for Textual dev tools.
    Returns:
        The aiohttp AppRunner instance.
    """
    self.debug = debug
    self.initialize_logging()

    if self.debug:
      log.info("Running in debug mode. You may use textual dev tools.")

    self.app = await self._make_app()

    self.app.router.add_get("/favicon.ico", self.favicon)
    self.app.router.add_post("/api/command-server-wake", self.command_server_wake)
    self.runner = web.AppRunner(self.app)
    await self.runner.setup()
    self.site = web.TCPSite(self.runner, self.host, self.port)
    await self.site.start()
    log.info("Textual server running on http://%s:%d", self.host, self.port)

    return self.runner
