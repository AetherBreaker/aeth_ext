# Standard library imports
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors import FATAL_EVENT
from aeth_ext.settings import BaseSettings
from aeth_ext.shared_log_processor.server.dispatch import DISPATCH_LOGGER
from aeth_ext.shared_log_processor.server.id_registry import ClientIdRegistry
from aeth_ext.shared_log_processor.server.reader_server import LogRecordServer
from aeth_ext.shared_log_processor.server.writer_thread import LogWriterThread

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.shared_log_processor.server.dispatch import WriterItem

logger = getLogger(__name__)


settings = BaseSettings.get_settings()


async def main(
  log_queue: Queue[WriterItem],
  host: str = "localhost",
  port: int = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Path = settings.log_loc_folder,
) -> None:

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
