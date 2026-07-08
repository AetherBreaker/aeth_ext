# Standard library imports
import asyncio
import atexit
from collections.abc import Callable, Coroutine
from logging import getLogger
from typing import Any, ClassVar

# Third party imports
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

# First party imports
from aeth_ext.command_server.decorators import COMMAND_ATTR, PARAMS_MODEL_ATTR
from aeth_ext.command_server.local_registry import register as registry_register, unregister as registry_unregister
from aeth_ext.command_server.protocol import (
  CommandInvocation,
  CommandMeta,
  CommandResponse,
  DiscoveryPayload,
  decode_message,
  encode_message,
)

logger = getLogger(__name__)

__all__ = ["CommandServerBase"]


type CommandHandler = Callable[..., Coroutine[Any, Any, Any]]


class CommandServerBase:
  """Base class for programs that expose remotely invocable commands over a FastAPI WebSocket.

  Subclass this inside a program, define ``program_name`` and ``port`` class
  attributes, and decorate async methods with
  :func:`aeth_ext.command_server.command` to register them:

  ```python
  class MyAppCommands(CommandServerBase):
    program_name = "my_app"
    port = 9030

    @command
    async def reload_config(self) -> None: ...

    @command(description="Fetch a status summary.")
    async def status(self, verbose: bool = False) -> dict: ...
  ```

  Call :meth:`start` from the program's async startup sequence; it launches
  uvicorn as a background asyncio task and returns immediately.

  When running inside Docker/Coolify, the service must declare labels so the
  command client can discover it:

  ```yaml
  labels:
    aeth.command_server.enabled: "true"
    aeth.command_server.port: "9030"
    aeth.command_server.name: "my_app"
  ```

  Outside Docker the server registers itself in a local registry file instead,
  which the client uses as its discovery fallback.
  """

  program_name: ClassVar[str]
  port: ClassVar[int]

  # Per-subclass registry: command name -> (meta, params model, unbound method)
  _command_registry: ClassVar[dict[str, tuple[CommandMeta, type[BaseModel], CommandHandler]]]

  def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)
    registry: dict[str, tuple[CommandMeta, type[BaseModel], CommandHandler]] = {}
    # Walk the full MRO so commands on intermediate bases are inherited.
    for klass in reversed(cls.__mro__):
      for attr in vars(klass).values():
        meta: CommandMeta | None = getattr(attr, COMMAND_ATTR, None)
        if meta is not None:
          registry[meta.name] = (meta, getattr(attr, PARAMS_MODEL_ATTR), attr)
    cls._command_registry = registry

  def __init__(self) -> None:
    if not hasattr(type(self), "program_name") or not hasattr(type(self), "port"):
      raise TypeError(f"{type(self).__name__} must define 'program_name' and 'port' class attributes")
    self._uvicorn_server: uvicorn.Server | None = None
    self._serve_task: asyncio.Task[None] | None = None

  @property
  def discovery_payload(self) -> DiscoveryPayload:
    return DiscoveryPayload(
      program_name=self.program_name,
      commands=tuple(meta for meta, _, _ in self._command_registry.values()),
    )

  def _build_app(self) -> FastAPI:
    app = FastAPI(title=f"{self.program_name} command server")

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
      await websocket.accept()
      await websocket.send_bytes(encode_message(self.discovery_payload))
      pending: set[asyncio.Task[None]] = set()
      try:
        while True:
          data = await websocket.receive_bytes()
          task = asyncio.create_task(self._handle_invocation(websocket, data))
          pending.add(task)
          task.add_done_callback(pending.discard)
      except WebSocketDisconnect:
        logger.debug("Command client disconnected from %r", self.program_name)
      finally:
        for task in pending:
          task.cancel()

    return app

  async def _handle_invocation(self, websocket: WebSocket, data: bytes) -> None:
    """Decode, validate, dispatch a single invocation and send back its response."""
    request_id = ""
    try:
      message = decode_message(data)
      if not isinstance(message, CommandInvocation):
        raise TypeError(f"Expected a command invocation, got {type(message).__name__}")
      request_id = message.request_id

      entry = self._command_registry.get(message.command)
      if entry is None:
        raise LookupError(f"Unknown command: {message.command!r}")
      _, params_model, handler = entry

      params = params_model.model_validate(message.params)
      result = await handler(self, **dict(params))
      response = CommandResponse(request_id=request_id, result=result)
    except asyncio.CancelledError, KeyboardInterrupt:
      raise
    except ValidationError as exc:
      response = CommandResponse(request_id=request_id, error=f"Invalid parameters: {exc}")
    except Exception as exc:
      logger.exception("Command invocation failed on %r", self.program_name)
      response = CommandResponse(request_id=request_id, error=f"{type(exc).__name__}: {exc}")

    try:
      await websocket.send_bytes(encode_message(response))
    except WebSocketDisconnect, RuntimeError:
      logger.debug("Client disconnected before response for request %r could be sent", request_id)

  async def start(self, host: str = "0.0.0.0") -> None:
    """Start the command server as a background asyncio task (non-blocking).

    Intended to be awaited once from the host program's async startup
    sequence. Registers this program in the local registry file so
    non-Docker clients can discover it, and installs an atexit hook to
    clean that registration up on shutdown.
    """
    if self._serve_task is not None:
      raise RuntimeError("Command server is already running")

    config = uvicorn.Config(self._build_app(), host=host, port=self.port, log_level="warning")
    self._uvicorn_server = uvicorn.Server(config)
    self._serve_task = asyncio.create_task(self._uvicorn_server.serve(), name=f"{self.program_name}-command-server")

    registry_register(self.program_name, "localhost", self.port)
    atexit.register(registry_unregister, self.program_name)
    logger.info("Command server %r listening on %s:%d", self.program_name, host, self.port)

  async def stop(self) -> None:
    """Gracefully stop the command server and remove the local registration."""
    registry_unregister(self.program_name)
    atexit.unregister(registry_unregister)
    if self._uvicorn_server is not None:
      self._uvicorn_server.should_exit = True
    if self._serve_task is not None:
      await self._serve_task
    self._uvicorn_server = None
    self._serve_task = None
