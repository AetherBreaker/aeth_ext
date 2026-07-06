# Standard library imports
from asyncio import create_task, sleep
from datetime import datetime
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from sys import platform
from typing import TYPE_CHECKING, NoReturn

# Third party imports
from rich.console import Console

# First party imports
from aeth_ext.errors import FATAL_EVENT, handle_fatal_exc_async
from aeth_ext.shared_log_processor.server.dispatch import DISPATCH_LOGGER
from aeth_ext.shared_log_processor.server.id_registry import ClientIdRegistry
from aeth_ext.shared_log_processor.server.reader_server import LogRecordServer
from aeth_ext.shared_log_processor.server.writer_thread import LogWriterThread
from aeth_ext.shared_log_processor.settings import Settings
from aeth_ext.shared_log_processor.web_viewer.server import InLoopServer

if TYPE_CHECKING:
  # Standard library imports

  # Standard library imports
  from collections.abc import Callable

  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.shared_log_processor.server.dispatch import WriterItem

logger = getLogger(__name__)

RICH_CONSOLE = Console(
  width=None if platform == "win32" else 165,
  log_time=platform == "win32",
)
PROJECT_NAME = "aeth_ext.shared_log_processor"

settings = Settings.get_settings()

FAVICON_PATH = Path.cwd() / "favicon.ico"


if not __debug__:
  # Heartbeat file for health checks
  HEARTBEAT_FILE = settings.log_loc_folder / "heartbeat.txt"

  def write_heartbeat():
    """Write current timestamp to heartbeat file for health monitoring."""
    try:
      HEARTBEAT_FILE.write_text(datetime.now(settings.tz).isoformat())
    except Exception as e:
      logger.error("Failed to write heartbeat", exc_info=e)
else:

  def write_heartbeat():
    pass


@handle_fatal_exc_async
async def run_periodic(interval: float, func: Callable[[], None]) -> NoReturn:
  """Run a function periodically at a specified interval."""
  while True:
    try:
      func()
    except Exception as e:
      logger.error("Error in periodic task", exc_info=e)
    await sleep(interval)


async def main(
  log_queue: Queue[WriterItem],
  host: str = "localhost",
  port: int = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Path = settings.log_loc_folder,
) -> None:
  RICH_CONSOLE.rule("[bold red]Booting...[/]", style="bold red")
  write_heartbeat()

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

  textual_server = InLoopServer(
    command="python -m aeth_ext.shared_log_processor.web_viewer",
    host=settings.file_serve_host,
    port=settings.file_serve_port,
    public_url=f"https://{settings.file_serve_public_domain}",
    favicon_path=FAVICON_PATH,
  )

  runner = await textual_server.serve_in_loop()

  logger.info(
    "Log processor running on %s:%d and serving web viewer on %s:%d",
    host,
    port,
    settings.file_serve_host,
    settings.file_serve_port,
  )

  periodic_heartbeat_task = create_task(run_periodic(30, write_heartbeat))

  RICH_CONSOLE.rule("[bold red]Boot Done[/]", style="bold red")

  async with tcp_server:
    try:
      # Block until something sets FATAL_EVENT (unhandled exception, signal,
      # external call) - this is where process-wide graceful shutdown logic lives.
      await FATAL_EVENT
    except KeyboardInterrupt:
      logger.info("Shutdown requested; stopping log processor")
    finally:
      FATAL_EVENT.set()

      # Stop accepting new connections; in-flight handlers run to completion.
      tcp_server.close()
      await tcp_server.wait_closed()
      await runner.cleanup()
      periodic_heartbeat_task.cancel()
      # Signal the writer thread to drain the queue and exit, then wait for it
      # so buffered records are flushed before the process ends.
      writer.join()
