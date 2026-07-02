# Standard library imports
from logging import getLogger
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors import FATAL_EVENT
from aeth_ext.settings import BaseSettings
from aeth_ext.shared_log_processor.dispatch import DISPATCH_LOGGER
from aeth_ext.shared_log_processor.log_socket_server import LogRecordServer
from aeth_ext.shared_log_processor.log_writer_thread import LogWriterThread

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.shared_log_processor.dispatch import WriterItem

logger = getLogger(__name__)


settings = BaseSettings.get_settings()


async def main(
  log_queue: Queue[WriterItem],
  host: str = "localhost",
  port: int = DEFAULT_TCP_LOGGING_PORT,
  log_dir: Path = settings.log_loc_folder,
) -> None:

  # The single writer thread owns every logging handler and performs all logging
  # IO; the asyncio server below only ever *produces* onto the shared queue.
  writer = LogWriterThread(log_queue, DISPATCH_LOGGER)
  writer.start()

  server = LogRecordServer(queue=log_queue, host=host, port=port, log_dir=log_dir)

  try:
    # TODO rework into a task so that shutdown and cleanup logic can be implemented here guarded behind FATAL_EVENT
    await server.start_server()
  except KeyboardInterrupt:
    logger.info("Shutdown requested; stopping log processor")
  finally:
    # Signal the writer thread to drain the queue and exit, then wait for it so
    # buffered records are flushed before the process ends.
    FATAL_EVENT.set()
    writer.join()
