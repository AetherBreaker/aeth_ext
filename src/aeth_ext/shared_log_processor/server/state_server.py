# Standard library imports
import asyncio
import orjson
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.shared_log_processor.server.writer_thread import LogWriterThread

logger = logging.getLogger(__name__)


class StateQueryServer:
  """Tiny localhost-only TCP server that vends a live snapshot of writer state.

  One JSON object is written per connection then the connection is closed.
  The payload shape matches what :class:`~aeth_ext.shared_log_processor.web_viewer
  .screens.log_picker.LogFileTree` expects::

      {
          "connected_programs": ["ProgramA", ...],
          "current_ids":        {"ProgramA": 12345, ...},
          "midnight_ids":       {"ProgramA": 12000, ...},
          "midnight_date":      "2026-07-06"          # or "" before first record
      }

  Because it only reads ``writer.state_snapshot()`` (an atomic reference read
  returning an already-built dict), it imposes zero locking overhead on the
  writer thread.
  """

  def __init__(
    self,
    writer: LogWriterThread,
    host: str = "127.0.0.1",
    port: int = 9021,
  ) -> None:
    self._writer = writer
    self._host = host
    self._port = port

  async def start(self) -> asyncio.Server:
    """Bind the socket and return the running server handle."""
    server = await asyncio.start_server(self._handle, self._host, self._port)
    logger.info("State-query server listening on %s:%d", self._host, self._port)
    return server

  async def _handle(self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
      payload = orjson.dumps(self._writer.state_snapshot())
      writer.write(payload)
      await writer.drain()
    except Exception:
      logger.debug("State-query handler error", exc_info=True)
    finally:
      writer.close()
      with suppress(OSError):
        await writer.wait_closed()
