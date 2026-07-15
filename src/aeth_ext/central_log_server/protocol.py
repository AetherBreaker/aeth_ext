# Standard library imports
import logging
import struct
from logging import Formatter, makeLogRecord
from typing import Any, Final

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
  """

  ok: bool
  error: str | None = None
  last_record_id: int | None = None
  last_received_at: float | None = None


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
  """
  Make a LogRecord whose attributes are defined by the specified dictionary,
  This function is useful for converting a logging event received over
  a socket connection (which is sent as a dictionary) into a LogRecord
  instance.
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
