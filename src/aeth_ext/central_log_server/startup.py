# Standard library imports
from asyncio import create_task
from contextlib import suppress
from importlib.metadata import version
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from pathlib import Path
from sys import platform
from typing import TYPE_CHECKING

# Third party imports
from rich import get_console

# First party imports
from aeth_ext.central_log_server import web_viewer
from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
from aeth_ext.central_log_server.server.reader_server import LogRecordServer
from aeth_ext.central_log_server.server.writer_thread import LogWriterThread
from aeth_ext.central_log_server.settings import Settings
from aeth_ext.central_log_server.web_viewer.server import InLoopServer
from aeth_ext.errors import FATAL_EVENT
from aeth_ext.monitoring import run_heartbeat_async, send_heartbeat_async

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from typing import Any

  # Third party imports
  from aiologic import SimpleQueue

  # First party imports
  from aeth_ext.central_log_server.server.dispatch import WriterItem

logger = getLogger(__name__)


settings = Settings.get_settings()

FAVICON_PATH = Path(__file__).parent / "web_viewer" / "favicon.ico"
TEMPLATES_PATH = Path(__file__).parent / "web_viewer" / "templates"

rich_console = get_console()

# The local file is only written outside dev, to avoid cluttering a local
# checkout. The healthchecks.io ping (settings.alerts_healthcheck_ping_url /
# alerts_healthcheck_pingkey) applies unconditionally instead of being
# debug-gated, since it's already a no-op unless a developer has explicitly
# configured it in their own .env.
HEARTBEAT_FILE = settings.log_loc_folder / "heartbeat.txt" if not __debug__ else None


async def main(
  log_queue: SimpleQueue[WriterItem],
  host: str = "0.0.0.0",
  port: int = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Path = settings.log_loc_folder,
  server_config: Mapping[str, Any] | None = None,
) -> None:
  rich_console.rule("[bold red]Booting...[/]", style="bold red")
  # Sent as early as possible, before the rest of boot -- if something hangs
  # during startup, the heartbeat still reflects "just started" until it goes
  # stale, rather than never having fired at all.
  #
  # The _async variant so the ping runs off the event loop: it is a synchronous
  # urlopen whose timeout does not cover DNS resolution, and this loop is the
  # one that goes on to accept every client connection. `slug` is deliberately
  # not passed -- send_heartbeat_async resolves HEARTBEAT_SLUG from this frame.
  await send_heartbeat_async(
    HEARTBEAT_FILE,
    ping_url=settings.alerts_healthcheck_ping_url,
    pingkey=settings.alerts_healthcheck_pingkey,
    tz=settings.tz,
    start=True,
  )

  # Loaded once and shared between the server (which reads it to build each
  # handshake ack) and the writer thread (the sole writer, which advances it
  # as records are dispatched and persists it to disk periodically).
  id_registry = ClientIdRegistry.load()

  # The single writer thread owns every logging handler and performs all logging
  # IO; the asyncio server below only ever *produces* onto the shared queue.
  writer = LogWriterThread(log_queue, id_registry, server_config=server_config)
  writer.start()

  server = LogRecordServer(queue=log_queue, id_registry=id_registry, host=host, port=port, log_dir=log_dir)

  tcp_server = await server.start_server()

  bound_host, bound_port = tcp_server.sockets[0].getsockname()[:2] if tcp_server.sockets else (host, port)
  rich_console.print(f"[bold green]aeth-ext[/] central log server v{version('aeth-ext')} listening on {bound_host}:{bound_port}")

  textual_server = InLoopServer(
    command=f"python -m {web_viewer.__name__}",
    host=settings.web_viewer_serve_host,
    port=settings.web_viewer_serve_port,
    title="Sweet Fire Tobacco Backend Log Viewer",
    public_url=(settings.web_viewer_public_url if platform != "win32" else f"http://localhost:{settings.web_viewer_serve_port}"),
    favicon_path=FAVICON_PATH,
    templates_path=TEMPLATES_PATH,
  )

  runner = await textual_server.serve_in_loop(__debug__)

  logger.info(
    "Log processor running on %s:%d and serving web viewer on %s:%d",
    host,
    port,
    settings.web_viewer_serve_host,
    settings.web_viewer_serve_port,
  )

  # send_start=False: the boot-time send_heartbeat() call above already sent
  # the one "start" ping for this run.
  periodic_heartbeat_task = create_task(
    run_heartbeat_async(
      HEARTBEAT_FILE,
      ping_url=settings.alerts_healthcheck_ping_url,
      pingkey=settings.alerts_healthcheck_pingkey,
      send_start=False,
      tz=settings.tz,
    )
  )

  rich_console.rule("[bold red]Boot Done[/]", style="bold red")

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
      # The heartbeat task also stops itself once FATAL_EVENT is set, but
      # cancelling here guarantees it doesn't linger even a moment longer.
      periodic_heartbeat_task.cancel()
      # Standard library imports
      from asyncio import CancelledError  # local import to keep shutdown path self-contained

      with suppress(CancelledError):
        await periodic_heartbeat_task
      # Signal the writer thread to drain the queue and exit, then wait for it
      # so buffered records are flushed before the process ends.
      writer.join()
