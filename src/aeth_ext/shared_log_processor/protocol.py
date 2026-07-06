# Standard library imports
import struct
from datetime import datetime
from logging import FileHandler, Filter, Formatter, Handler, getLogger, makeLogRecord
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from typing import Annotated, Any, Final, overload

# Third party imports
from cloudpickle import dumps, loads
from pydantic.config import ConfigDict
from pydantic.dataclasses import dataclass
from pydantic.functional_validators import BeforeValidator

# First party imports
from aeth_ext.logging.bases import NamedLogRecord
from aeth_ext.settings import BaseSettings
from aeth_ext.types import IsPydanticSlots

logger = getLogger(__name__)

# Length prefix shared by both the handshake and every log record:
# a 4-byte big-endian unsigned integer giving the size of the pickled payload.
# This matches the framing used by logging.handlers.SocketHandler.
LENGTH_STRUCT: Final[struct.Struct] = struct.Struct(">L")


pyd_config = ConfigDict(arbitrary_types_allowed=True)


settings = BaseSettings.get_settings()


@dataclass(config=pyd_config, slots=True)
class MiscDef(IsPydanticSlots):
  pickled_def: bytes
  cls_name: str
  args: tuple[Any, ...]
  kwargs: dict[str, Any]


@dataclass(config=pyd_config, slots=True)
class FormatterDef(MiscDef):
  _kind = "formatter"


@dataclass(config=pyd_config, slots=True)
class FilterDef(MiscDef):
  _kind = "filter"


@dataclass(config=pyd_config, slots=True)
class HandlerDef(MiscDef):
  project_name: str
  formatter: FormatterDef | None
  filters: tuple[FilterDef, ...] | None
  level: int | None = None
  startup_rollover: bool | None = None
  _kind = "handler"


@overload
def construct_cls_from_def(definition: HandlerDef) -> Handler: ...


@overload
def construct_cls_from_def(definition: FormatterDef) -> Formatter: ...


@overload
def construct_cls_from_def(definition: FilterDef) -> Filter: ...


def construct_cls_from_def(definition: HandlerDef | FormatterDef | FilterDef) -> Handler | Formatter | Filter:  # noqa: C901
  """Reconstruct a logging class from its pickled definition.

  The definition is a :class:`HandlerDef`, :class:`FormatterDef`, or :class:`FilterDef` that was sent by a client to the server. It contains the pickled class, its name, and the args/kwargs used to construct it.
  """

  match definition:
    case HandlerDef(formatter=formatter_def, filters=filter_defs):
      handler_cls = unpickle_def(definition)

      # handle the files paths of any file handlers, which need to be made relative to the servers
      # log storage directory
      if issubclass(handler_cls, FileHandler):
        args = list(definition.args)
        path: str | Path = definition.kwargs.get("filename") or args[0]

        # We must assume that the path is not absolute
        if isinstance(path, Path):
          path = str(path)

        new_path = settings.log_loc_folder / definition.project_name / path

        new_path.parent.mkdir(parents=True, exist_ok=True)

        if "filename" in definition.kwargs:
          definition.kwargs["filename"] = new_path
        else:
          args[0] = new_path
          definition.args = tuple(args)

      formatter = construct_cls_from_def(formatter_def) if formatter_def else None
      filters = tuple(construct_cls_from_def(f) for f in filter_defs) if filter_defs else ()
      instance: Handler = handler_cls(*definition.args, **definition.kwargs)
      if formatter:
        instance.setFormatter(formatter)
      for f in filters:
        instance.addFilter(f)

      if definition.startup_rollover and issubclass(handler_cls, BaseRotatingHandler):
        try:
          instance.doRollover()  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
          pass
      instance.setLevel(definition.level or 0)
      return instance

    case FormatterDef():
      formatter_cls = unpickle_def(definition)
      return formatter_cls(*definition.args, **definition.kwargs)

    case FilterDef():
      filter_cls = unpickle_def(definition)
      return filter_cls(*definition.args, **definition.kwargs)

    case _:
      raise ValueError(f"Unknown definition type: {type(definition)}")  # pyright: ignore[reportUnreachable]


@overload
def unpickle_def(definition: HandlerDef) -> type[Handler]: ...


@overload
def unpickle_def(definition: FormatterDef) -> type[Formatter]: ...


@overload
def unpickle_def(definition: FilterDef) -> type[Filter]: ...


def unpickle_def(definition: HandlerDef | FormatterDef | FilterDef) -> type[Handler | Formatter | Filter]:
  """Reconstruct a logging class from its pickled definition.

  The definition is a :class:`HandlerDef`, :class:`FormatterDef`, or
  :class:`FilterDef` that was sent by a client to the server. It contains the
  pickled class, its name, and the args/kwargs used to construct it.
  """
  return loads(definition.pickled_def)


@dataclass(config=pyd_config, slots=True, frozen=True)
class LoggingHandshake(IsPydanticSlots):
  """Identifying message a client sends to the log server.

  It is exchanged exactly once, immediately after the socket connects and
  before any log records are streamed. The server uses it to dynamically
  create and register a dedicated set of file handlers for the connecting
  program.
  """

  handlers: tuple[Annotated[Handler, BeforeValidator(construct_cls_from_def)], ...]
  program_name: str
  logging_base_name: str | None = None

  def pprint(self, level: int) -> None:
    """Pretty-print the handshake for debugging purposes."""
    logger.log(level, "Handshake Details:")
    logger.log(level, "  program_name:      %s", self.program_name)
    logger.log(level, "  logging_base_name: %s", self.logging_base_name)
    logger.log(level, "  handlers:")
    for idx, handler in enumerate(self.handlers):
      logger.log(level, "    Handler %d:", idx + 1)
      logger.log(level, "      Classname: %s", handler.__class__.__name__)
      logger.log(level, "      Level: %s", handler.level)

      if isinstance(handler, FileHandler):
        logger.log(level, "      Filename: %s", Path(handler.baseFilename))


@dataclass(config=pyd_config, slots=True, frozen=True)
class ClientLoggingHandshake(IsPydanticSlots):
  """Identifying message a client sends to the log server.

  It is exchanged exactly once, immediately after the socket connects and
  before any log records are streamed. The server uses it to dynamically
  create and register a dedicated set of file handlers for the connecting
  program.
  """

  handlers: tuple[HandlerDef, ...]
  program_name: str
  logging_base_name: str | None = None


@dataclass(config=pyd_config, slots=True, frozen=True)
class HandshakeAck(IsPydanticSlots):
  """Reply the server sends immediately after processing a client's handshake.

  Lets the client resume precisely after a reconnect: ``last_record_id`` is the
  highest ``record_id`` the server has ever received for this
  ``program_name`` (``None`` if it has never seen this program before), and
  ``last_received_at`` is that record's own ``created`` timestamp (i.e. when
  the client originally emitted it, not when the server received it), which
  the client uses to pick which of its own date-segregated history file(s) to
  search if the record has already been evicted from its in-memory buffer.
  """

  last_record_id: int | None
  last_received_at: datetime | None


def encode_packet(obj: object) -> bytes:
  """Pickle ``obj`` and prepend its 4-byte big-endian length.

  The result is framed identically to ``logging.handlers.SocketHandler``
  messages so the server can read handshakes and log records the same way.
  """
  data = dumps(obj)
  return LENGTH_STRUCT.pack(len(data)) + data


class TaggedLogRecord(NamedLogRecord):
  """A LogRecord with a ``name`` attribute that is always set to the logger's name.

  This is useful for log records received over a socket connection, where the
  logger's name may not be set correctly.
  """

  source_name: str | None
  record_id: int | None

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    super().__init__(*args, **kwargs)
    self.source_name = None
    self.record_id = None


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
