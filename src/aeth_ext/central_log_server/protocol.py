"""Wire protocol shared by the central log server and its clients: framing, handshake messages, and record (de)serialisation."""

# Standard library imports
import logging
import struct
from logging import Formatter, makeLogRecord
from typing import Any, Final, Literal

# Third party imports
import orjson
from pydantic.config import ConfigDict
from pydantic.dataclasses import dataclass

# First party imports
from aeth_ext.logging.bases import TaggedLogRecord
from aeth_ext.types import IsPydanticSlots

# Length prefix shared by both the handshake and every log record:
# a 4-byte big-endian unsigned integer giving the size of the JSON payload.
# This matches the framing used by logging.handlers.SocketHandler.
LENGTH_STRUCT: Final[struct.Struct] = struct.Struct(">L")


pyd_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(config=pyd_config, slots=True, frozen=True)
class ClientHandshake(IsPydanticSlots):
  """Identifying message a client sends to the log server.

  It is exchanged exactly once, immediately after the socket connects and
  before any log records are streamed. ``config`` is a standard dict-based
  logging configuration (see `aeth_ext.logging.config.models.LoggingConfigModel`)
  that the server applies into a private logging hierarchy dedicated to this
  program; any ``logdir://`` values in it are resolved server-side beneath the
  server's per-program log directory.
  """

  program_name: str
  config: dict[str, Any]


@dataclass(config=pyd_config, slots=True, frozen=True)
class HandshakeAck(IsPydanticSlots):
  """Reply the server sends immediately after processing a client's handshake.

  ``ok`` reports whether the handshake's remote config was accepted; when it is
  ``False``, ``error`` describes why and the server closes the connection - the
  client should treat this as a fatal configuration error.

  On success the ack lets the client resume precisely after a reconnect:
  ``last_record_id`` is the highest ``record_id`` the server has ever received
  for this ``program_name`` (``None`` if it has never seen this program
  before), and ``last_received_at`` is that record's own ``created`` timestamp
  (i.e. when the client originally emitted it, not when the server received
  it), which the client uses to pick which of its own date-segregated history
  file(s) to search if the record has already been evicted from its in-memory
  buffer.

  Sent the moment the config passes validation (D-A1) - **before** the writer
  thread has actually applied it. It therefore does *not* mean the config is
  live yet; see :class:`ApplySuccess`/:class:`ApplyFailure` for that. ``type``
  is the framing discriminator every server message carries (D-E5): the first
  inbound message on a connection is always a `HandshakeAck`, but a client
  reading again afterward must check ``type`` rather than assume the shape.
  """

  ok: bool
  error: str | None = None
  last_record_id: int | None = None
  last_received_at: float | None = None
  type: Literal["handshake_ack"] = "handshake_ack"


@dataclass(config=pyd_config, slots=True, frozen=True)
class ApplySuccess(IsPydanticSlots):
  """Out-of-band confirmation that the writer thread applied this connection's config (D-E2a).

  Sent exactly once, after the `HandshakeAck`, the moment the writer thread
  finishes applying this program's config. Purely informational - it lets a
  client watcher stop waiting for a possible `ApplyFailure` instead of staying
  alive for the rest of the connection - and changes nothing about how records
  are handled.
  """

  type: Literal["apply_success"] = "apply_success"


@dataclass(config=pyd_config, slots=True, frozen=True)
class ApplyFailure(IsPydanticSlots):
  """Out-of-band rejection sent when the writer thread could not apply this connection's config.

  Unlike a `HandshakeAck` rejection (a validation failure - the client's
  fault), this fires only after the config was already validated and the
  optimistic `HandshakeAck` already sent (D-E2); it means the config was
  well-formed but the environment refused it at apply time (e.g. EACCES, disk
  full). No fallback is attempted - the connection is rejected outright so the
  client's own alert-then-shutdown path (D-E6) gets a maintainer to fix it.
  The server closes the connection immediately after sending this.
  """

  error: str
  type: Literal["apply_failure"] = "apply_failure"


_SERVER_MESSAGE_TYPES: Final[dict[str, type[HandshakeAck | ApplySuccess | ApplyFailure]]] = {
  "handshake_ack": HandshakeAck,
  "apply_success": ApplySuccess,
  "apply_failure": ApplyFailure,
}


def decode_server_message(payload: bytes) -> HandshakeAck | ApplySuccess | ApplyFailure | None:
  """Decode a length-stripped payload from the server into its concrete message type.

  Dispatches on the ``type`` discriminator field every server message carries
  (D-E5). Returns ``None`` for anything malformed, including an unrecognised
  or missing ``type`` - a missing discriminator is never treated as an
  implicit `HandshakeAck`; old-client compatibility is not a concern here,
  every current server build stamps a real ``type`` value.
  """
  try:
    obj: object = orjson.loads(payload)
  except orjson.JSONDecodeError:
    return None
  if not isinstance(obj, dict):
    return None
  msg_type = obj.get("type")
  if not isinstance(msg_type, str):
    return None
  cls = _SERVER_MESSAGE_TYPES.get(msg_type)
  if cls is None:
    return None
  try:
    return cls(**obj)
  except TypeError, ValueError:
    return None


def encode_json_packet(obj: Any) -> bytes:
  """Serialise ``obj`` with orjson and prepend its 4-byte big-endian length.

  The result is framed identically to ``logging.handlers.SocketHandler``
  messages so the server can read handshakes and log records the same way.
  Dataclass instances (e.g. `ClientHandshake` / `HandshakeAck`) are serialised
  natively by orjson; non-JSON-native values fall back to ``str``.
  """
  data = orjson.dumps(obj, default=str)
  return LENGTH_STRUCT.pack(len(data)) + data


def make_log_record(received: dict[str, Any], source_name: str) -> TaggedLogRecord:
  """Make a LogRecord whose attributes are defined by the specified dictionary.

  Useful for converting a logging event received over a socket connection
  (which is sent as a dictionary) into a LogRecord instance. The result is
  stamped with *source_name*.
  """
  record: TaggedLogRecord = makeLogRecord(received)  # pyright: ignore[reportAssignmentType]

  record.source_name = source_name

  return record


# Reused only for its ``formatException`` implementation when baking a record's
# traceback into ``exc_text`` prior to serialisation.
_EXC_FORMATTER: Final[Formatter] = Formatter()


def record_to_payload(record: logging.LogRecord) -> dict[str, Any]:
  """Return a JSON-serialisable copy of *record*'s ``__dict__``.

  The record itself is left untouched (unlike the historical in-place
  approach): the interpolated message is baked into ``msg``, ``args`` is
  cleared, and any live ``exc_info`` traceback is rendered into ``exc_text``
  and then dropped so the payload contains only primitives. Non-JSON-native
  values (e.g. a ``Path`` in ``source_path`` or stray ``extra=`` objects) are
  handled by the caller's ``orjson.dumps(..., default=str)``.
  """
  data = dict(record.__dict__)
  data["msg"] = record.getMessage()
  data["args"] = None
  exc_info = data.get("exc_info")
  if exc_info:
    if not data.get("exc_text"):
      data["exc_text"] = _EXC_FORMATTER.formatException(exc_info)
    data["exc_info"] = None
  return data


def payload_to_record(data: dict[str, Any]) -> TaggedLogRecord:
  """Rebuild a :class:`TaggedLogRecord` from a :func:`record_to_payload` dict.

  Bypasses ``__init__`` (and its path parsing) via ``__new__`` because every
  attribute the record needs is already present in *data*.
  """
  record = TaggedLogRecord.__new__(TaggedLogRecord)
  record.__dict__.update(data)
  return record
