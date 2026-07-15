"""Tests for `aeth_ext.central_log_server.protocol`."""

# Standard library imports
import logging
import sys

# Third party imports
import orjson
import pytest
from pydantic import ValidationError

# First party imports
from aeth_ext.central_log_server.protocol import (
  LENGTH_STRUCT,
  ClientHandshake,
  HandshakeAck,
  encode_json_packet,
  make_log_record,
  payload_to_record,
  record_to_payload,
)
from aeth_ext.logging.bases import TaggedLogRecord

_HEADER_SIZE = 4
_RECORD_ID = 17
_LAST_RECORD_ID = 41
_LAST_RECEIVED_AT = 1_700_000_000.5


def _make_record(msg: str = "hello %s", args: tuple[object, ...] | None = ("world",)) -> TaggedLogRecord:
  return TaggedLogRecord("prog.module", logging.INFO, __file__, 1, msg, args, None)


class TestHandshakeModels:
  def test_client_handshake_defaults(self):
    handshake = ClientHandshake(program_name="prog", config={"version": 1})

    assert handshake.program_name == "prog"
    assert handshake.config == {"version": 1}

  def test_client_handshake_requires_config(self):
    with pytest.raises(ValidationError):
      ClientHandshake(program_name="prog")  # pyright: ignore[reportCallIssue]

  def test_handshake_ack_defaults(self):
    ack = HandshakeAck(ok=True)

    assert ack.ok is True
    assert ack.error is None
    assert ack.last_record_id is None
    assert ack.last_received_at is None

  def test_handshake_ack_ignores_unknown_fields(self):
    """Unknown keys are tolerated (forward compatibility), not stored."""
    ack = HandshakeAck(ok=True, bogus="ignored")  # pyright: ignore[reportCallIssue]

    assert ack.ok is True
    assert not hasattr(ack, "bogus")

  def test_handshake_ack_requires_ok(self):
    with pytest.raises(ValidationError):
      HandshakeAck(error="missing ok")  # pyright: ignore[reportCallIssue]


class TestEncodeJsonPacket:
  def test_length_prefix_matches_payload(self):
    packet = encode_json_packet({"a": 1})

    (length,) = LENGTH_STRUCT.unpack(packet[:_HEADER_SIZE])
    assert length == len(packet) - _HEADER_SIZE
    assert orjson.loads(packet[_HEADER_SIZE:]) == {"a": 1}

  def test_dataclasses_serialise_natively(self):
    ack = HandshakeAck(ok=False, error="bad config")
    packet = encode_json_packet(ack)

    decoded = orjson.loads(packet[_HEADER_SIZE:])
    assert decoded == {"ok": False, "error": "bad config", "last_record_id": None, "last_received_at": None}

  def test_non_json_values_fall_back_to_str(self):
    packet = encode_json_packet({"value": object()})

    decoded = orjson.loads(packet[_HEADER_SIZE:])
    assert isinstance(decoded["value"], str)


class TestRecordPayloadRoundTrip:
  def test_message_baked_and_args_cleared(self):
    record = _make_record()
    payload = record_to_payload(record)

    assert payload["msg"] == "hello world"
    assert payload["args"] is None
    # The original record is left untouched.
    assert record.msg == "hello %s"
    assert record.args == ("world",)

  def test_exc_info_rendered_to_exc_text(self):
    try:
      raise ValueError("boom")
    except ValueError:
      record = TaggedLogRecord("prog", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())

    payload = record_to_payload(record)

    assert payload["exc_info"] is None
    assert isinstance(payload["exc_text"], str)
    assert "ValueError: boom" in payload["exc_text"]

  def test_payload_to_record_round_trip(self):
    record = _make_record()
    record.record_id = _RECORD_ID
    payload = orjson.loads(orjson.dumps(record_to_payload(record), default=str))

    rebuilt = payload_to_record(payload)

    assert isinstance(rebuilt, TaggedLogRecord)
    assert rebuilt.getMessage() == "hello world"
    assert rebuilt.name == "prog.module"
    assert rebuilt.levelno == logging.INFO
    assert rebuilt.record_id == _RECORD_ID


class TestMakeLogRecord:
  def test_stamps_source_name(self):
    payload = record_to_payload(_make_record())

    record = make_log_record(payload, "prog")

    assert record.source_name == "prog"
    assert record.getMessage() == "hello world"
    # The record name is preserved verbatim (no base-name stripping).
    assert record.name == "prog.module"
