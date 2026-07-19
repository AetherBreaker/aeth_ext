# Standard library imports
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

# Third party imports
from aiohttp import web
from aiohttp.web import FileResponse, Request
from textual_serve.app_service import AppService
from textual_serve.server import Server

if TYPE_CHECKING:
  # Standard library imports
  from os import PathLike


log = logging.getLogger("textual-serve")

_HTTP_PORT = 80
_HTTPS_PORT = 443

_BASE_PROJECT_NAME = "aeth_ext.central-log-web-viewer"


class SessionAppService(AppService):
  """AppService that injects a session-unique ID into the subprocess environment.

  Each browser connection gets a unique ``AETH_WEB_SESSION_ID`` env var whose
  value is the raw ``app_service_id`` UUID hex.  The web_viewer's
  ``LoggingConfig.get_default_remote_config`` uses this to build session-specific
  log filenames (e.g. ``<uuid>_debug.log``, ``<uuid>_textual_debug.log``) that
  all land inside the single shared ``aeth_ext.central-log-web-viewer/`` directory
  on the log server, avoiding per-session subdirectory clutter.
  """

  @override
  def _build_environment(self, width: int = 80, height: int = 24) -> dict[str, str]:
    env = super()._build_environment(width=width, height=height)
    env["AETH_WEB_SESSION_ID"] = self.app_service_id
    return env


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

  async def serve_in_loop(self, debug: bool = False) -> web.AppRunner:
    """Serve the Textual application in an already-running event loop.

    Args:
        debug: Enable debug mode for Textual dev tools.
    Returns:
        The aiohttp AppRunner instance.
    """
    self.debug = debug

    if self.debug:
      log.info("Running in debug mode. You may use textual dev tools.")

    self.app = await self._make_app()

    self.app.router.add_get("/favicon.ico", self.favicon)
    self.runner = web.AppRunner(self.app)
    await self.runner.setup()
    self.site = web.TCPSite(self.runner, self.host, self.port)
    await self.site.start()
    log.info("Textual server running on http://%s:%d", self.host, self.port)

    return self.runner

  @override
  async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
    """Override to use SessionAppService so each connection logs to its own directory."""
    websocket = web.WebSocketResponse(heartbeat=15)

    try:
      width = int(request.query.get("width", "80"))
    except ValueError:
      width = 80
    try:
      height = int(request.query.get("height", "24"))
    except ValueError:
      height = 24

    app_service: SessionAppService | None = None
    try:
      await websocket.prepare(request)
      app_service = SessionAppService(
        self.command,
        write_bytes=websocket.send_bytes,
        write_str=websocket.send_str,
        close=websocket.close,  # pyright: ignore[reportArgumentType]
        download_manager=self.download_manager,
        debug=self.debug,
      )
      await app_service.start(width, height)
      try:
        await self._process_messages(websocket, app_service)
      finally:
        await app_service.stop()

    except asyncio.CancelledError:
      await websocket.close()

    except Exception as error:
      log.exception(error)

    finally:
      if app_service is not None:
        await app_service.stop()

    return websocket
