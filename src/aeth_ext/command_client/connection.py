# Standard library imports
import asyncio
from logging import getLogger
from typing import Any
from uuid import uuid4

# Third party imports
from aiohttp import ClientError, ClientSession, ClientWebSocketResponse, WSMsgType

# First party imports
from aeth_ext.command_client.discovery import ServerEndpoint
from aeth_ext.command_server.protocol import (
  CommandInvocation,
  CommandMeta,
  CommandResponse,
  DiscoveryPayload,
  decode_message,
  encode_message,
)

logger = getLogger(__name__)

__all__ = ["ServerConnection"]

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
DEFAULT_INVOKE_TIMEOUT = 30.0


class ServerConnection:
  """A persistent WebSocket connection to a single command server.

  Handles the discovery handshake, request/response correlation via
  ``request_id``, and automatic reconnection with exponential backoff.
  """

  def __init__(self, session: ClientSession, endpoint: ServerEndpoint) -> None:
    self._session = session
    self.endpoint = endpoint
    self._ws: ClientWebSocketResponse | None = None
    self._discovery: DiscoveryPayload | None = None
    self._pending: dict[str, asyncio.Future[CommandResponse]] = {}
    self._receive_task: asyncio.Task[None] | None = None
    self._reconnect_task: asyncio.Task[None] | None = None
    self._closed = False

  @property
  def connected(self) -> bool:
    return self._ws is not None and not self._ws.closed

  @property
  def program_name(self) -> str:
    """The name the server reported in its discovery payload (falls back to endpoint name)."""
    return self._discovery.program_name if self._discovery else self.endpoint.name

  @property
  def available_commands(self) -> tuple[CommandMeta, ...]:
    return self._discovery.commands if self._discovery else ()

  async def connect(self) -> None:
    """Open the WebSocket, receive the discovery payload, and start the receive loop."""
    url = f"ws://{self.endpoint.host}:{self.endpoint.port}/ws"
    self._ws = await self._session.ws_connect(url)

    msg = await self._ws.receive()
    if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
      raise ConnectionError(f"Expected discovery payload from {url}, got {msg.type.name}")
    discovery = decode_message(msg.data)
    if not isinstance(discovery, DiscoveryPayload):
      raise ConnectionError(f"Expected discovery payload from {url}, got {type(discovery).__name__}")
    self._discovery = discovery

    self._receive_task = asyncio.create_task(self._receive_loop(), name=f"{self.program_name}-cmd-recv")
    logger.info("Connected to command server %r (%d commands)", self.program_name, len(discovery.commands))

  async def _receive_loop(self) -> None:
    """Route incoming responses to their pending futures until the socket closes."""
    ws = self._ws
    assert ws is not None
    try:
      async for msg in ws:
        if msg.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
          continue
        try:
          message = decode_message(msg.data)
        except ValueError:
          logger.warning("Undecodable message from %r; ignoring", self.program_name)
          continue
        if isinstance(message, CommandResponse):
          future = self._pending.pop(message.request_id, None)
          if future is not None and not future.done():
            future.set_result(message)
    finally:
      self._fail_pending(ConnectionError(f"Connection to {self.program_name!r} lost"))
      if not self._closed:
        self._start_reconnect()

  def _fail_pending(self, exc: Exception) -> None:
    for future in self._pending.values():
      if not future.done():
        future.set_exception(exc)
    self._pending.clear()

  def _start_reconnect(self) -> None:
    if self._reconnect_task is None or self._reconnect_task.done():
      self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name=f"{self.program_name}-cmd-reconnect")

  async def _reconnect_loop(self) -> None:
    delay = RECONNECT_INITIAL_DELAY
    while not self._closed:
      await asyncio.sleep(delay)
      try:
        await self.connect()
      except OSError, ClientError, ConnectionError:
        delay = min(delay * 2, RECONNECT_MAX_DELAY)
        logger.debug("Reconnect to %r failed; retrying in %.0fs", self.program_name, delay)
      else:
        return

  async def invoke(self, command: str, params: dict[str, Any] | None = None, *, timeout: float = DEFAULT_INVOKE_TIMEOUT) -> Any:
    """Invoke ``command`` on the server, returning its result.

    For commands that don't return a value, waits only for the server's
    acknowledgement response (which also surfaces any remote error).
    Raises :class:`ConnectionError` if not connected, :class:`LookupError`
    for unknown commands, and :class:`RuntimeError` for remote failures.
    """
    if self._ws is None or self._ws.closed:
      raise ConnectionError(f"Not connected to command server {self.program_name!r}")

    known = {meta.name for meta in self.available_commands}
    if command not in known:
      raise LookupError(f"Command server {self.program_name!r} does not accept command {command!r}")

    request_id = uuid4().hex
    future: asyncio.Future[CommandResponse] = asyncio.get_running_loop().create_future()
    self._pending[request_id] = future

    invocation = CommandInvocation(request_id=request_id, command=command, params=params or {})
    try:
      await self._ws.send_bytes(encode_message(invocation))
      response = await asyncio.wait_for(future, timeout)
    finally:
      self._pending.pop(request_id, None)

    if response.error is not None:
      raise RuntimeError(f"Command {command!r} on {self.program_name!r} failed: {response.error}")
    return response.result

  async def close(self) -> None:
    """Permanently close this connection (no reconnect)."""
    self._closed = True
    if self._reconnect_task is not None:
      self._reconnect_task.cancel()
    if self._ws is not None:
      await self._ws.close()
    if self._receive_task is not None:
      try:
        await self._receive_task
      except asyncio.CancelledError:
        pass
    self._fail_pending(ConnectionError(f"Connection to {self.program_name!r} closed"))
