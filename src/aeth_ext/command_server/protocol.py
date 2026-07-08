# Standard library imports
from logging import getLogger
from typing import Any

# Third party imports
from orjson import dumps, loads
from pydantic import TypeAdapter
from pydantic.config import ConfigDict
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.types import IsPydanticSlots

logger = getLogger(__name__)

__all__ = [
  "CommandInvocation",
  "CommandMeta",
  "CommandResponse",
  "DiscoveryPayload",
  "decode_message",
  "encode_message",
]


pyd_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(config=pyd_config, slots=True)
class CommandMeta(IsPydanticSlots):
  """Metadata describing a single command exposed by a command server.

  Included in the :class:`DiscoveryPayload` sent to clients on connection so
  they know what commands exist, what parameters they take, and whether a
  result should be awaited.
  """

  name: str
  description: str
  params_schema: dict[str, Any]
  returns_value: bool


@dataclass(config=pyd_config, slots=True)
class DiscoveryPayload(IsPydanticSlots):
  """First message sent by a command server to every connecting client."""

  program_name: str
  commands: tuple[CommandMeta, ...]


@dataclass(config=pyd_config, slots=True)
class CommandInvocation(IsPydanticSlots):
  """A request from a client to execute a named command on the server."""

  request_id: str
  command: str
  params: dict[str, Any]


@dataclass(config=pyd_config, slots=True)
class CommandResponse(IsPydanticSlots):
  """The server's reply to a :class:`CommandInvocation`.

  Exactly one of ``result`` or ``error`` is meaningful: if ``error`` is not
  ``None`` the invocation failed and ``result`` must be ignored.
  """

  request_id: str
  result: Any = None
  error: str | None = None


type WireMessage = DiscoveryPayload | CommandInvocation | CommandResponse

# Adapters keyed by the "type" discriminator embedded in each encoded message.
_ADAPTERS: dict[str, TypeAdapter[Any]] = {
  "discovery": TypeAdapter(DiscoveryPayload),
  "invocation": TypeAdapter(CommandInvocation),
  "response": TypeAdapter(CommandResponse),
}

_TYPE_TAGS: dict[type, str] = {
  DiscoveryPayload: "discovery",
  CommandInvocation: "invocation",
  CommandResponse: "response",
}


def encode_message(message: WireMessage) -> bytes:
  """Serialize a wire message to orjson bytes with a ``type`` discriminator."""
  tag = _TYPE_TAGS[type(message)]
  payload = _ADAPTERS[tag].dump_python(message)
  payload["type"] = tag
  return dumps(payload)


def decode_message(data: bytes | str) -> WireMessage:
  """Deserialize orjson bytes back into the appropriate wire message type."""
  payload: dict[str, Any] = loads(data)
  tag = payload.pop("type")
  adapter = _ADAPTERS.get(tag)
  if adapter is None:
    raise ValueError(f"Unknown wire message type: {tag!r}")
  return _ADAPTERS[tag].validate_python(payload)
