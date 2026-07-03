# Standard library imports
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
from aiohttp.web import Application, AppRunner, FileResponse, Request, TCPSite
from rich import get_console

# First party imports
from aeth_ext.errors import FATAL_EVENT
from aeth_ext.shared_log_processor.server.dispatch import DISPATCH_LOGGER
from aeth_ext.shared_log_processor.server.id_registry import ClientIdRegistry
from aeth_ext.shared_log_processor.server.reader_server import LogRecordServer
from aeth_ext.shared_log_processor.server.writer_thread import LogWriterThread
from aeth_ext.shared_log_processor.settings import Settings

if TYPE_CHECKING:
  # Standard library imports

  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.shared_log_processor.server.dispatch import WriterItem

logger = getLogger(__name__)

RICH_CONSOLE = get_console()

settings = Settings.get_settings()

FAVICON_PATH = Path.cwd() / "favicon.ico"


async def main(
  log_queue: Queue[WriterItem],
  host: str = "localhost",
  port: int = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Path = settings.log_loc_folder,
) -> None:
  RICH_CONSOLE.rule("[bold red]Booting...[/]", style="bold red")

  # Loaded once and shared between the server (which reads it to build each
  # handshake ack) and the writer thread (the sole writer, which advances it
  # as records are dispatched and persists it to disk periodically).
  id_registry = ClientIdRegistry.load()

  # The single writer thread owns every logging handler and performs all logging
  # IO; the asyncio server below only ever *produces* onto the shared queue.
  writer = LogWriterThread(log_queue, DISPATCH_LOGGER, id_registry)
  writer.start()

  server = LogRecordServer(queue=log_queue, id_registry=id_registry, host=host, port=port, log_dir=log_dir)

  tcp_server = await server.start_server()

  app = Application()

  async def favicon(request: Request):
    return FileResponse(FAVICON_PATH)

  app.router.add_get("/favicon.ico", favicon)
  app.router.add_static("/", settings.log_loc_folder, show_index=True, follow_symlinks=True, append_version=True)
  runner = AppRunner(app)
  await runner.setup()
  site = TCPSite(runner, settings.file_serve_host, settings.file_serve_port)
  await site.start()

  RICH_CONSOLE.rule("[bold red]Boot Done[/]", style="bold red")

  async with tcp_server:
    try:
      # Block until something sets FATAL_EVENT (unhandled exception, signal,
      # external call) - this is where process-wide graceful shutdown logic lives.
      await FATAL_EVENT
    except KeyboardInterrupt:
      logger.info("Shutdown requested; stopping log processor")
    finally:
      # Stop accepting new connections; in-flight handlers run to completion.
      tcp_server.close()
      await tcp_server.wait_closed()
      # Signal the writer thread to drain the queue and exit, then wait for it
      # so buffered records are flushed before the process ends.
      FATAL_EVENT.set()
      writer.join()
