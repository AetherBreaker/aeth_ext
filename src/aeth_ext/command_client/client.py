# Standard library imports
import asyncio
from logging import getLogger
from typing import TYPE_CHECKING, Any

# Third party imports
from aiohttp import ClientError, ClientSession

# First party imports
from aeth_ext.command_client.connection import DEFAULT_INVOKE_TIMEOUT, ServerConnection
from aeth_ext.command_client.discovery import ServerEndpoint, discover

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.command_server.protocol import CommandMeta

logger = getLogger(__name__)

__all__ = ["CommandClient"]


class CommandClient:
  """Discovers and maintains connections to every command server in the stack.

  Usage:

  ```python
  client = CommandClient()
  await client.connect_all()
  result = await client.invoke("my_app", "status", verbose=True)
  await client.close()
  ```
  """

  def __init__(self) -> None:
    self._session: ClientSession | None = None
    self._connections: dict[str, ServerConnection] = {}

  @property
  def available_servers(self) -> tuple[str, ...]:
    """Names of servers with a currently live connection."""
    return tuple(name for name, conn in self._connections.items() if conn.connected)

  def get_connection(self, server_name: str) -> ServerConnection | None:
    return self._connections.get(server_name)

  def commands_for(self, server_name: str) -> tuple[CommandMeta, ...]:
    """The commands a given server reported accepting, or empty if unknown."""
    conn = self._connections.get(server_name)
    return conn.available_commands if conn else ()

  async def connect_all(self) -> None:
    """Discover all command servers and connect to each, tolerating individual failures.

    Safe to call again later to pick up newly discovered servers; existing
    connections are left untouched.
    """
    if self._session is None:
      self._session = ClientSession()

    endpoints = await discover()
    new = [ep for ep in endpoints if ep.name not in self._connections]
    if not new:
      logger.debug("Command server discovery found no new servers")
      return

    results = await asyncio.gather(*(self._connect_one(ep) for ep in new), return_exceptions=True)
    for endpoint, result in zip(new, results, strict=False):
      if isinstance(result, BaseException):
        logger.warning("Failed to connect to command server %r at %s:%d: %s", endpoint.name, endpoint.host, endpoint.port, result)

  async def _connect_one(self, endpoint: ServerEndpoint) -> None:
    assert self._session is not None
    conn = ServerConnection(self._session, endpoint)
    try:
      await conn.connect()
    except OSError, ClientError, ConnectionError:
      await conn.close()
      raise
    self._connections[conn.program_name] = conn

  async def invoke(self, server_name: str, command: str, *, timeout: float = DEFAULT_INVOKE_TIMEOUT, **params: Any) -> Any:
    """Invoke ``command`` on the named server, returning its result (if any)."""
    conn = self._connections.get(server_name)
    if conn is None:
      raise LookupError(f"No known command server named {server_name!r}")
    return await conn.invoke(command, params, timeout=timeout)

  async def close(self) -> None:
    """Close all connections and the underlying HTTP session."""
    await asyncio.gather(*(conn.close() for conn in self._connections.values()), return_exceptions=True)
    self._connections.clear()
    if self._session is not None:
      await self._session.close()
      self._session = None
