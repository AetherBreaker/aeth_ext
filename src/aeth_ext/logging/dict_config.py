# Copyright 2001-2023 by Vinay Sajip. All Rights Reserved.
#
# Permission to use, copy, modify, and distribute this software and its
# documentation for any purpose and without fee is hereby granted,
# provided that the above copyright notice appear in all copies and that
# both that copyright notice and this permission notice appear in
# supporting documentation, and that the name of Vinay Sajip
# not be used in advertising or publicity pertaining to distribution
# of the software without specific, written prior permission.
# VINAY SAJIP DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE, INCLUDING
# ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL
# VINAY SAJIP BE LIABLE FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR
# ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER
# IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
# OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""
Configuration functions for the logging package for Python. The core package
is based on PEP 282 and comments thereto in comp.lang.python, and influenced
by Apache's log4j system.

Copyright (C) 2001-2022 Vinay Sajip. All Rights Reserved.

To use, simply 'import logging' and log away!
"""

# Standard library imports
import errno
import functools
import logging
import logging.handlers
import queue
import re
import struct
import threading
import traceback
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, cast, override

# Third party imports
import orjson
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
  # Standard library imports
  import io
  import socket
  from collections.abc import Callable, Iterable, Mapping, MutableMapping
  from typing import SupportsIndex

DEFAULT_LOGGING_CONFIG_PORT = 9030

RESET_ERROR = errno.ECONNRESET

# Size, in bytes, of the length prefix sent ahead of a configuration payload.
LENGTH_PREFIX_SIZE = 4

# typeshed does not stub logging's private internals (_lock, _handlers,
# _handlerList, _checkLevel), but this module legitimately needs them to
# replicate stdlib logging.config behaviour. Pyright resolves module
# attribute types strictly from typeshed's stub for every access site, so a
# bare `logging._lock: Type` declaration elsewhere does not propagate - each
# usage would still need its own ignore comment. Instead, monkey-patch these
# internals onto module-level names here (single ignore comment per name)
# and use those names everywhere else in this file.
_logging_lock: threading.RLock = logging._lock  # type: ignore[attr-defined]
_logging_handlers: MutableMapping[str, logging.Handler] = logging._handlers  # type: ignore[attr-defined]
_logging_handler_list: list[Any] = logging._handlerList  # type: ignore[attr-defined]
_check_level: Callable[[int | str], int] = logging._checkLevel  # type: ignore[attr-defined]

#
#   The following code implements a socket listener for on-the-fly
#   reconfiguration of logging.
#
#   _listener holds the server object doing the listening
_listener = None


class _LoggingConfigBaseModel(BaseModel):
  """
  Common base for all logging-config schema models. Centralizes the shared
  `populate_by_name` setting so subclasses only need to declare `extra`.
  """

  model_config = ConfigDict(populate_by_name=True)


class FormatterConfig(_LoggingConfigBaseModel):
  """Schema for a single entry in the ``formatters`` section of a logging config."""

  model_config = ConfigDict(extra="allow")

  class_: Annotated[
    str | None,
    Field(
      alias="class",
      description=(
        "Dotted import path to a `logging.Formatter` subclass (or any callable "
        "returning a formatter-like object) used to construct this formatter. "
        "If omitted, `logging.Formatter` is used."
      ),
    ),
  ] = None
  format: Annotated[
    str | None,
    Field(
      description="The format string passed as the `fmt` argument to the formatter constructor, e.g. `'%(levelname)s:%(name)s:%(message)s'`."
    ),
  ] = None
  datefmt: Annotated[
    str | None,
    Field(description="The `strftime`-style date/time format string used to render `%(asctime)s` in log records."),
  ] = None
  style: Annotated[
    Literal["%", "{", "$"],
    Field(
      description="The format-string style: `'%'` for printf-style, `'{'` for `str.format`-style, or `'$'` for `string.Template`-style."
    ),
  ] = "%"
  validate_field: Annotated[
    bool | None,
    Field(
      alias="validate",
      description="If `True` (the default in `logging.Formatter`), validate that `format` is compatible with the selected `style` at construction time.",
    ),
  ] = None
  defaults: Annotated[
    dict[str, Any] | None,
    Field(description="A mapping of default values substituted for missing log record fields referenced in `format`."),
  ] = None
  factory: Annotated[
    Any,
    Field(
      alias="()",
      description=(
        "A custom factory (callable or dotted import path) used to construct this "
        "formatter instead of `class_`/`logging.Formatter`. Any additional keys in "
        "this object are passed to the factory as keyword arguments."
      ),
    ),
  ] = None


class FilterConfig(_LoggingConfigBaseModel):
  """Schema for a single entry in the ``filters`` section of a logging config."""

  model_config = ConfigDict(extra="allow")

  name: Annotated[
    str | None,
    Field(
      description="The logger name prefix used by the default `logging.Filter`; only records from loggers at or below this name pass through."
    ),
  ] = None
  factory: Annotated[
    Any,
    Field(
      alias="()",
      description=(
        "A custom factory (callable or dotted import path) used to construct this "
        "filter instead of the default `logging.Filter`. Any additional keys in "
        "this object are passed to the factory as keyword arguments."
      ),
    ),
  ] = None


class HandlerConfig(_LoggingConfigBaseModel):
  """Schema for a single entry in the ``handlers`` section of a logging config."""

  model_config = ConfigDict(extra="allow")

  class_: Annotated[
    str | None,
    Field(
      alias="class",
      description="Dotted import path to a `logging.Handler` subclass used to construct this handler, e.g. `'logging.StreamHandler'`.",
    ),
  ] = None
  factory: Annotated[
    Any,
    Field(
      alias="()",
      description=(
        "A custom factory (callable or dotted import path) used to construct this "
        "handler instead of `class_`. Any additional keys in this object are "
        "passed to the factory as keyword arguments."
      ),
    ),
  ] = None
  formatter: Annotated[
    str | None,
    Field(description="The name of an entry in the top-level `formatters` section to attach to this handler."),
  ] = None
  level: Annotated[
    str | int | None,
    Field(description="The minimum log level (name such as `'INFO'`, or numeric value) this handler will process."),
  ] = None
  filters: Annotated[
    list[str] | None,
    Field(description="Names of entries in the top-level `filters` section to attach to this handler."),
  ] = None
  target: Annotated[
    str | None,
    Field(
      description="The name of another entry in the top-level `handlers` section to use as the target, e.g. for `logging.handlers.MemoryHandler`."
    ),
  ] = None


class LoggerConfig(_LoggingConfigBaseModel):
  """Schema for a single entry in the ``loggers`` section of a logging config."""

  model_config = ConfigDict(extra="forbid")

  level: Annotated[
    str | int | None,
    Field(description="The minimum log level (name such as `'DEBUG'`, or numeric value) this logger will process."),
  ] = None
  propagate: Annotated[
    bool | None,
    Field(description="Whether log records handled by this logger should also propagate to ancestor loggers."),
  ] = None
  filters: Annotated[
    list[str] | None,
    Field(description="Names of entries in the top-level `filters` section to attach to this logger."),
  ] = None
  handlers: Annotated[
    list[str] | None,
    Field(description="Names of entries in the top-level `handlers` section to attach to this logger."),
  ] = None


class RootLoggerConfig(_LoggingConfigBaseModel):
  """Schema for the ``root`` section of a logging config."""

  model_config = ConfigDict(extra="forbid")

  level: Annotated[
    str | int | None,
    Field(description="The minimum log level (name such as `'WARNING'`, or numeric value) the root logger will process."),
  ] = None
  filters: Annotated[
    list[str] | None,
    Field(description="Names of entries in the top-level `filters` section to attach to the root logger."),
  ] = None
  handlers: Annotated[
    list[str] | None,
    Field(description="Names of entries in the top-level `handlers` section to attach to the root logger."),
  ] = None


class LoggingConfigModel(_LoggingConfigBaseModel):
  """Top-level schema for a dict-based logging configuration."""

  model_config = ConfigDict(extra="forbid")

  version: Annotated[
    Literal[1],
    Field(description="Schema version identifier. Must be `1`; this is the only version currently supported."),
  ]
  formatters: Annotated[
    dict[str, FormatterConfig],
    Field(description="A mapping of formatter name to its configuration, defining the layout of log messages."),
  ] = {}
  filters: Annotated[
    dict[str, FilterConfig],
    Field(description="A mapping of filter name to its configuration, used to selectively allow/deny log records."),
  ] = {}
  handlers: Annotated[
    dict[str, HandlerConfig],
    Field(description="A mapping of handler name to its configuration, defining where log records are emitted to."),
  ] = {}
  loggers: Annotated[
    dict[str, LoggerConfig],
    Field(description="A mapping of logger name to its configuration, for configuring non-root loggers."),
  ] = {}
  root: Annotated[RootLoggerConfig | None, Field(description="Configuration for the root logger.")] = None
  incremental: Annotated[
    bool,
    Field(
      description=(
        "If `True`, this configuration only updates handler levels and "
        "logger levels/propagation for already-configured objects, rather "
        "than performing a full reconfiguration."
      ),
    ),
  ] = False
  disable_existing_loggers: Annotated[
    bool,
    Field(
      description=(
        "If `True` (the default), loggers not present in this configuration "
        "that existed prior to configuring are disabled (unless they are "
        "children of a logger that is present)."
      ),
    ),
  ] = True


def _resolve(name: str) -> Any:
  """Resolve a dotted name to a global object."""
  parts = name.split(".")
  used = parts.pop(0)
  found = __import__(used)
  for n in parts:
    used = used + "." + n
    try:
      found = getattr(found, n)
    except AttributeError:
      __import__(used)
      found = getattr(found, n)
  return found


def _handle_existing_loggers(existing: list[str], child_loggers: list[str], disable_existing: bool) -> None:
  """
  When (re)configuring logging, handle loggers which were in the previous
  configuration but are not in the new configuration. There's no point
  deleting them as other threads may continue to hold references to them;
  and by disabling them, you stop them doing any logging.

  However, don't disable children of named loggers, as that's probably not
  what was intended by the user. Also, allow existing loggers to NOT be
  disabled if disable_existing is false.
  """
  root = logging.root
  for log in existing:
    logger = root.manager.loggerDict[log]
    if log in child_loggers:
      if not isinstance(logger, logging.PlaceHolder):
        logger.setLevel(logging.NOTSET)
        logger.handlers = []
        logger.propagate = True
    else:
      cast("logging.Logger", logger).disabled = disable_existing


def _find_child_loggers(existing: list[str], qn: str) -> list[str]:
  """Find existing loggers which are children of the *qn* named logger."""
  prefixed = qn + "."
  pflen = len(prefixed)
  return [name for name in existing if name[:pflen] == prefixed]


def _clear_existing_handlers():
  """Clear and close existing handlers"""
  _logging_handlers.clear()
  logging.shutdown(_logging_handler_list[:])
  del _logging_handler_list[:]


IDENTIFIER = re.compile("^[a-z_][a-z0-9_]*$", re.I)


def valid_ident(s: str) -> bool:
  m = IDENTIFIER.match(s)
  if not m:
    raise ValueError(f"Not a valid Python identifier: {s!r}")
  return True


class ConvertingMixin:
  """For ConvertingXXX's, this mixin class provides common functions"""

  configurator: BaseConfigurator = cast("BaseConfigurator", None)

  def convert_with_key(self, key: Any, value: Any, replace: bool = True) -> Any:
    result = self.configurator.convert(value)
    # If the converted value is different, save for next time
    if value is not result:
      if replace:
        cast("MutableMapping[Any, Any]", self)[key] = result
      if type(result) in (ConvertingDict, ConvertingList, ConvertingTuple):
        result.parent = self
        result.key = key
    return result

  def convert(self, value: Any) -> Any:
    result = self.configurator.convert(value)
    if value is not result:
      if type(result) in (ConvertingDict, ConvertingList, ConvertingTuple):
        result.parent = self
    return result


# The ConvertingXXX classes are wrappers around standard Python containers,
# and they serve to convert any suitable values in the container. The
# conversion converts base dicts, lists and tuples to their wrapped
# equivalents, whereas strings which match a conversion format are converted
# appropriately.
#
# Each wrapper should have a configurator attribute holding the actual
# configurator to use for conversion.


class ConvertingDict(dict, ConvertingMixin):
  """A converting dictionary wrapper."""

  @override
  def __getitem__(self, key: Any) -> Any:
    value = dict.__getitem__(self, key)
    return self.convert_with_key(key, value)

  @override
  def get(self, key: Any, default: Any = None) -> Any:
    value = dict.get(self, key, default)
    return self.convert_with_key(key, value)

  @override
  def pop(self, key: Any, default: Any = None) -> Any:
    value = dict.pop(self, key, default)
    return self.convert_with_key(key, value, replace=False)


class ConvertingList(list, ConvertingMixin):
  """A converting list wrapper."""

  @override
  def __getitem__(self, key: SupportsIndex | slice) -> Any:
    value = list.__getitem__(self, key)
    return self.convert_with_key(key, value)

  @override
  def pop(self, idx: SupportsIndex = -1) -> Any:
    value = list.pop(self, idx)
    return self.convert(value)


class ConvertingTuple(tuple, ConvertingMixin):
  """A converting tuple wrapper."""

  @override
  def __getitem__(self, key: SupportsIndex | slice) -> Any:
    value = tuple.__getitem__(self, key)
    # Can't replace a tuple entry.
    return self.convert_with_key(key, value, replace=False)


class BaseConfigurator:
  """
  The configurator base class which defines some useful defaults.
  """

  CONVERT_PATTERN = re.compile(r"^(?P<prefix>[a-z]+)://(?P<suffix>.*)$")

  WORD_PATTERN = re.compile(r"^\s*(\w+)\s*")
  DOT_PATTERN = re.compile(r"^\.\s*(\w+)\s*")
  INDEX_PATTERN = re.compile(r"^\[([^\[\]]*)\]\s*")
  DIGIT_PATTERN = re.compile(r"^\d+$")

  value_converters: ClassVar[dict[str, str]] = {
    "ext": "ext_convert",
    "cfg": "cfg_convert",
  }

  # We might want to use a different one, e.g. importlib
  importer = staticmethod(__import__)

  def __init__(self, config: Mapping[str, Any]) -> None:
    self.config = ConvertingDict(config)
    self.config.configurator = self

  def resolve(self, s: str) -> Any:
    """
    Resolve strings to objects using standard import and attribute
    syntax.
    """
    name = s.split(".")
    used = name.pop(0)
    try:
      found = self.importer(used)
      for frag in name:
        used += "." + frag
        try:
          found = getattr(found, frag)
        except AttributeError:
          self.importer(used)
          found = getattr(found, frag)
      return found
    except ImportError as e:
      v = ValueError(f"Cannot resolve {s!r}: {e}")
      raise v from e

  def ext_convert(self, value: str) -> Any:
    """Default converter for the ext:// protocol."""
    return self.resolve(value)

  def cfg_convert(self, value: str) -> Any:
    """Default converter for the cfg:// protocol."""
    rest = value
    m = self.WORD_PATTERN.match(rest)
    if m is None:
      raise ValueError(f"Unable to convert {value!r}")
    else:
      rest = rest[m.end() :]
      d = self.config[m.groups()[0]]
      # print d, rest
      while rest:
        m = self.DOT_PATTERN.match(rest)
        if m:
          d = d[m.groups()[0]]
        else:
          m = self.INDEX_PATTERN.match(rest)
          if m:
            idx = m.groups()[0]
            if not self.DIGIT_PATTERN.match(idx):
              d = d[idx]
            else:
              try:
                n = int(idx)  # try as number first (most likely)
                d = d[n]
              except TypeError:
                d = d[idx]
        if m:
          rest = rest[m.end() :]
        else:
          raise ValueError(f"Unable to convert {value!r} at {rest!r}")
    # rest should be empty
    return d

  def convert(self, value: Any) -> Any:
    """
    Convert values to an appropriate type. dicts, lists and tuples are
    replaced by their converting alternatives. Strings are checked to
    see if they have a conversion format and are converted if they do.
    """
    if not isinstance(value, ConvertingDict) and isinstance(value, dict):
      value = ConvertingDict(value)
      value.configurator = self
    elif not isinstance(value, ConvertingList) and isinstance(value, list):
      value = ConvertingList(value)
      value.configurator = self
    elif not isinstance(value, ConvertingTuple) and isinstance(value, tuple) and not hasattr(value, "_fields"):
      value = ConvertingTuple(value)
      value.configurator = self
    elif isinstance(value, str):  # str for py3k
      m = self.CONVERT_PATTERN.match(value)
      if m:
        d = m.groupdict()
        prefix = d["prefix"]
        converter = self.value_converters.get(prefix, None)
        if converter:
          suffix = d["suffix"]
          converter = getattr(self, converter)
          value = converter(suffix)
    return value

  def configure_custom(self, config: MutableMapping[str, Any]) -> Any:
    """Configure an object with a user-supplied factory."""
    c = config.pop("()")
    if not callable(c):
      c = self.resolve(c)
    # Check for valid identifiers
    kwargs = {k: config[k] for k in config if (k != "." and valid_ident(k))}
    result = c(**kwargs)
    props = config.pop(".", None)
    if props:
      for name, value in props.items():
        setattr(result, name, value)
    return result

  def as_tuple(self, value: Any) -> Any:
    """Utility function which converts lists to tuples."""
    if isinstance(value, list):
      value = tuple(value)
    return value


def _is_queue_like_object(obj: Any) -> bool:
  """Check that *obj* implements the Queue API."""
  if isinstance(obj, (queue.Queue, queue.SimpleQueue)):
    return True
  # defer importing multiprocessing as much as possible
  # Standard library imports
  from multiprocessing.queues import Queue as MPQueue

  if isinstance(obj, MPQueue):
    return True
  # Depending on the multiprocessing start context, we cannot create
  # a multiprocessing.managers.BaseManager instance 'mm' to get the
  # runtime type of mm.Queue() or mm.JoinableQueue() (see gh-119819).
  #
  # Since we only need an object implementing the Queue API, we only
  # do a protocol check, but we do not use typing.runtime_checkable()
  # and typing.Protocol to reduce import time (see gh-121723).
  #
  # Ideally, we would have wanted to simply use strict type checking
  # instead of a protocol-based type checking since the latter does
  # not check the method signatures.
  #
  # Note that only 'put_nowait' and 'get' are required by the logging
  # queue handler and queue listener (see gh-124653) and that other
  # methods are either optional or unused.
  minimal_queue_interface = ["put_nowait", "get"]
  return all(callable(getattr(obj, method, None)) for method in minimal_queue_interface)


class DictConfigurator(BaseConfigurator):
  """
  Configure logging using a dictionary-like object to describe the
  configuration.
  """

  def __init__(self, config: Mapping[str, Any]) -> None:
    """Validate *config* against `LoggingConfigModel` before storing it."""
    LoggingConfigModel.model_validate(config)
    super().__init__(config)

  def configure(self):
    """Do the configuration."""

    config = self.config
    incremental = config.pop("incremental", False)
    with _logging_lock:
      if incremental:
        self._configure_incremental(config)
      else:
        self._configure_full(config)

  def _configure_incremental(self, config: MutableMapping[str, Any]) -> None:
    """Handle an incremental (partial) reconfiguration."""
    handlers = config.get("handlers", {})
    for name in handlers:
      if name not in _logging_handlers:
        raise ValueError(f"No handler found with name {name!r}")
      try:
        handler = _logging_handlers[name]
        handler_config = handlers[name]
        level = handler_config.get("level", None)
        if level:
          handler.setLevel(_check_level(level))
      except Exception as e:
        raise ValueError(f"Unable to configure handler {name!r}") from e
    loggers = config.get("loggers", {})
    for name in loggers:
      try:
        self.configure_logger(name, loggers[name], incremental=True)
      except Exception as e:
        raise ValueError(f"Unable to configure logger {name!r}") from e
    root = config.get("root", None)
    if root:
      try:
        self.configure_root(root, incremental=True)
      except Exception as e:
        raise ValueError("Unable to configure root logger") from e

  def _configure_full(self, config: MutableMapping[str, Any]) -> None:
    """Handle a full (non-incremental) reconfiguration."""
    disable_existing = config.pop("disable_existing_loggers", True)

    _clear_existing_handlers()

    # Do formatters first - they don't refer to anything else
    self._configure_formatters(config)
    # Next, do filters - they don't refer to anything else, either
    self._configure_filters(config)
    # Next, do handlers - they refer to formatters and filters
    handlers = self._configure_handlers_section(config)
    # Next, do loggers - they refer to handlers and filters
    self._configure_loggers_section(config, handlers, disable_existing)

    # And finally, do the root logger
    root = config.get("root", None)
    if root:
      try:
        self.configure_root(root)
      except Exception as e:
        raise ValueError("Unable to configure root logger") from e

  def _configure_formatters(self, config: MutableMapping[str, Any]) -> None:
    """Configure the 'formatters' section of a full configuration."""
    formatters = config.get("formatters", {})
    for name in formatters:
      try:
        formatters[name] = self.configure_formatter(formatters[name])
      except Exception as e:
        raise ValueError(f"Unable to configure formatter {name!r}") from e

  def _configure_filters(self, config: MutableMapping[str, Any]) -> None:
    """Configure the 'filters' section of a full configuration."""
    filters = config.get("filters", {})
    for name in filters:
      try:
        filters[name] = self.configure_filter(filters[name])
      except Exception as e:
        raise ValueError(f"Unable to configure filter {name!r}") from e

  def _configure_handlers_section(self, config: MutableMapping[str, Any]) -> dict[str, logging.Handler]:
    """Configure the 'handlers' section of a full configuration."""
    # As handlers can refer to other handlers, sort the keys
    # to allow a deterministic order of configuration
    handlers = config.get("handlers", {})
    deferred = []
    for name in sorted(handlers):
      try:
        handler = self.configure_handler(handlers[name])
        handler.name = name
        handlers[name] = handler
      except Exception as e:
        if " not configured yet" in str(e.__cause__):
          deferred.append(name)
        else:
          raise ValueError(f"Unable to configure handler {name!r}") from e

    # Now do any that were deferred
    for name in deferred:
      try:
        handler = self.configure_handler(handlers[name])
        handler.name = name
        handlers[name] = handler
      except Exception as e:
        raise ValueError(f"Unable to configure handler {name!r}") from e
    return handlers

  def _configure_loggers_section(
    self, config: MutableMapping[str, Any], handlers: dict[str, logging.Handler], disable_existing: bool
  ) -> None:
    """Configure the 'loggers' section of a full configuration."""
    # we don't want to lose the existing loggers,
    # since other threads may have pointers to them.
    # existing is set to contain all existing loggers,
    # and as we go through the new configuration we
    # remove any which are configured. At the end,
    # what's left in existing is the set of loggers
    # which were in the previous configuration but
    # which are not in the new configuration.
    existing = list(logging.root.manager.loggerDict.keys())
    # The list needs to be sorted so that we can
    # avoid disabling child loggers of explicitly
    # named loggers. With a sorted list it is easier
    # to find the child loggers.
    existing.sort()
    # We'll keep the list of existing loggers
    # which are children of named loggers here...
    child_loggers = []
    # now set up the new ones...
    loggers = config.get("loggers", {})
    for name in loggers:
      if name in existing:
        child_loggers.extend(_find_child_loggers(existing, name))
        existing.remove(name)
      try:
        self.configure_logger(name, loggers[name])
      except Exception as e:
        raise ValueError(f"Unable to configure logger {name!r}") from e

    # Disable any old loggers. There's no point deleting
    # them as other threads may continue to hold references
    # and by disabling them, you stop them doing any logging.
    # However, don't disable children of named loggers, as that's
    # probably not what was intended by the user.
    _handle_existing_loggers(existing, child_loggers, disable_existing)

  def configure_formatter(self, config: MutableMapping[str, Any]) -> logging.Formatter:
    """Configure a formatter from a dictionary."""
    if "()" in config:
      factory = config["()"]  # for use in exception handler
      try:
        result = self.configure_custom(config)
      except TypeError as te:
        if "'format'" not in str(te):
          raise
        # logging.Formatter and its subclasses expect the `fmt`
        # parameter instead of `format`. Retry passing configuration
        # with `fmt`.
        config["fmt"] = config.pop("format")
        config["()"] = factory
        result = self.configure_custom(config)
    else:
      fmt = config.get("format", None)
      dfmt = config.get("datefmt", None)
      style = config.get("style", "%")
      cname = config.get("class", None)
      defaults = config.get("defaults", None)

      if not cname:
        c = logging.Formatter
      else:
        c = _resolve(cname)

      kwargs = {}

      # Add defaults only if it exists.
      # Prevents TypeError in custom formatter callables that do not
      # accept it.
      if defaults is not None:
        kwargs["defaults"] = defaults

      # A TypeError would be raised if "validate" key is passed in with a formatter callable
      # that does not accept "validate" as a parameter
      if "validate" in config:  # if user hasn't mentioned it, the default will be fine
        result = c(fmt, dfmt, style, config["validate"], **kwargs)
      else:
        result = c(fmt, dfmt, style, **kwargs)

    return result

  def configure_filter(self, config: MutableMapping[str, Any]) -> logging.Filter:
    """Configure a filter from a dictionary."""
    if "()" in config:
      result = self.configure_custom(config)
    else:
      name = config.get("name", "")
      result = logging.Filter(name)
    return result

  def add_filters(self, filterer: logging.Filterer, filters: Iterable[Any]) -> None:
    """Add filters to a filterer from a list of names."""
    for f in filters:
      try:
        filter_: Any
        if callable(f) or callable(getattr(f, "filter", None)):
          filter_ = f
        else:
          filter_ = self.config["filters"][f]
        filterer.addFilter(filter_)
      except Exception as e:
        raise ValueError(f"Unable to add filter {f!r}") from e

  def _configure_queue_handler(self, klass: type[logging.handlers.QueueHandler], **kwargs: Any) -> logging.handlers.QueueHandler:
    if "queue" in kwargs:
      q = kwargs.pop("queue")
    else:
      q = queue.Queue()  # unbounded

    rhl = kwargs.pop("respect_handler_level", False)
    lklass = kwargs.pop("listener", logging.handlers.QueueListener)
    handlers = kwargs.pop("handlers", [])

    listener = lklass(q, *handlers, respect_handler_level=rhl)
    handler = klass(q, **kwargs)
    handler.listener = listener
    return handler

  def configure_handler(self, config: MutableMapping[str, Any]) -> logging.Handler:
    """Configure a handler from a dictionary."""
    config_copy = dict(config)  # for restoring in case of error
    formatter = config.pop("formatter", None)
    if formatter:
      try:
        formatter = self.config["formatters"][formatter]
      except Exception as e:
        raise ValueError(f"Unable to set formatter {formatter!r}") from e
    level = config.pop("level", None)
    filters = config.pop("filters", None)
    if "()" in config:
      c = config.pop("()")
      factory = c if callable(c) else self.resolve(c)
    else:
      factory = self._resolve_class_handler_factory(config, config_copy)

    kwargs = {k: config[k] for k in config if (k != "." and valid_ident(k))}
    result = self._instantiate_handler(factory, kwargs)

    if formatter:
      result.setFormatter(formatter)
    if level is not None:
      result.setLevel(_check_level(level))
    if filters:
      self.add_filters(result, filters)
    props = config.pop(".", None)
    if props:
      for name, value in props.items():
        setattr(result, name, value)
    return result

  def _resolve_class_handler_factory(self, config: MutableMapping[str, Any], config_copy: Mapping[str, Any]) -> Callable[..., Any]:
    """Resolve the handler factory when configured via 'class' (not '()')."""
    cname: Any = config.pop("class")
    klass: Any = cname if callable(cname) else self.resolve(cname)
    if issubclass(klass, logging.handlers.MemoryHandler):
      self._configure_memory_handler(config, config_copy)
    elif issubclass(klass, logging.handlers.QueueHandler):
      self._configure_queue_handler_options(config, config_copy)
    elif issubclass(klass, logging.handlers.SMTPHandler) and "mailhost" in config:
      config["mailhost"] = self.as_tuple(config["mailhost"])
    elif issubclass(klass, logging.handlers.SysLogHandler) and "address" in config:
      config["address"] = self.as_tuple(config["address"])
    if issubclass(klass, logging.handlers.QueueHandler):
      return functools.partial(self._configure_queue_handler, klass)
    return klass

  def _configure_memory_handler(self, config: MutableMapping[str, Any], config_copy: Mapping[str, Any]) -> None:
    """Resolve MemoryHandler-specific config keys ('flushLevel', 'target')."""
    if "flushLevel" in config:
      config["flushLevel"] = _check_level(config["flushLevel"])
    if "target" in config:
      # Special case for handler which refers to another handler
      tn: Any = None
      try:
        tn = config["target"]
        th = self.config["handlers"][tn]
        if not isinstance(th, logging.Handler):
          config.update(config_copy)  # restore for deferred cfg
          raise TypeError("target not configured yet")
        config["target"] = th
      except Exception as e:
        raise ValueError(f"Unable to set target handler {tn!r}") from e

  def _configure_queue_handler_options(self, config: MutableMapping[str, Any], config_copy: Mapping[str, Any]) -> None:
    """Resolve QueueHandler-specific config keys ('queue', 'listener', 'handlers')."""
    # if 'handlers' not in config:
    # raise ValueError('No handlers specified for a QueueHandler')
    if "queue" in config:
      config["queue"] = self._resolve_queue_spec(config["queue"])
    if "listener" in config:
      config["listener"] = self._resolve_listener_spec(config["listener"])
    if "handlers" in config:
      config["handlers"] = self._resolve_queue_handlers(config, config_copy)

  def _resolve_queue_spec(self, qspec: Any) -> Any:
    """Resolve a 'queue' specifier to a queue-like object."""
    if isinstance(qspec, str):
      q = self.resolve(qspec)
      if not callable(q):
        raise TypeError(f"Invalid queue specifier {qspec!r}")
      return q()
    if isinstance(qspec, dict):
      if "()" not in qspec:
        raise TypeError(f"Invalid queue specifier {qspec!r}")
      return self.configure_custom(dict(qspec))
    if not _is_queue_like_object(qspec):
      raise TypeError(f"Invalid queue specifier {qspec!r}")
    return qspec

  def _resolve_listener_spec(self, lspec: Any) -> Any:
    """Resolve a 'listener' specifier to a QueueListener class or factory."""
    listener: Any
    if isinstance(lspec, type):
      if not issubclass(lspec, logging.handlers.QueueListener):
        raise TypeError(f"Invalid listener specifier {lspec!r}")
      return lspec
    if isinstance(lspec, str):
      listener = self.resolve(lspec)
      if isinstance(listener, type) and not issubclass(listener, logging.handlers.QueueListener):
        raise TypeError(f"Invalid listener specifier {lspec!r}")
    elif isinstance(lspec, dict):
      if "()" not in lspec:
        raise TypeError(f"Invalid listener specifier {lspec!r}")
      listener = self.configure_custom(dict(lspec))
    else:
      raise TypeError(f"Invalid listener specifier {lspec!r}")
    if not callable(listener):
      raise TypeError(f"Invalid listener specifier {lspec!r}")
    return listener

  def _resolve_queue_handlers(self, config: MutableMapping[str, Any], config_copy: Mapping[str, Any]) -> list[logging.Handler]:
    """Resolve the 'handlers' list referenced by a QueueHandler config."""
    hlist = []
    hn: Any = None
    try:
      for hn in config["handlers"]:
        h = self.config["handlers"][hn]
        if not isinstance(h, logging.Handler):
          config.update(config_copy)  # restore for deferred cfg
          raise TypeError(f"Required handler {hn!r} is not configured yet")
        hlist.append(h)
    except Exception as e:
      raise ValueError(f"Unable to set required handler {hn!r}") from e
    return hlist

  def _instantiate_handler(self, factory: Callable[..., Any], kwargs: MutableMapping[str, Any]) -> logging.Handler:
    """Instantiate a handler, retrying with the deprecated 'strm' kwarg name."""
    # When deprecation ends for using the 'strm' parameter, remove the
    # "except TypeError ..." handling below.
    try:
      return factory(**kwargs)
    except TypeError as te:
      if "'stream'" not in str(te):
        raise
      # The argument name changed from strm to stream
      # Retry with old name.
      # This is so that code can be used with older Python versions
      # (e.g. by Django)
      kwargs["strm"] = kwargs.pop("stream")
      result = factory(**kwargs)

      # Standard library imports
      import warnings

      warnings.warn(
        "Support for custom logging handlers with the 'strm' argument "
        "is deprecated and scheduled for removal in Python 3.16. "
        "Define handlers with the 'stream' argument instead.",
        DeprecationWarning,
        stacklevel=2,
      )
      return result

  def add_handlers(self, logger: logging.Logger, handlers: Iterable[str]) -> None:
    """Add handlers to a logger from a list of names."""
    for h in handlers:
      try:
        logger.addHandler(self.config["handlers"][h])
      except Exception as e:
        raise ValueError(f"Unable to add handler {h!r}") from e

  def common_logger_config(self, logger: logging.Logger, config: Mapping[str, Any], incremental: bool = False) -> None:
    """
    Perform configuration which is common to root and non-root loggers.
    """
    level = config.get("level", None)
    if level is not None:
      logger.setLevel(_check_level(level))
    if not incremental:
      # Remove any existing handlers
      for h in logger.handlers[:]:
        logger.removeHandler(h)
      handlers = config.get("handlers", None)
      if handlers:
        self.add_handlers(logger, handlers)
      filters = config.get("filters", None)
      if filters:
        self.add_filters(logger, filters)

  def configure_logger(self, name: str, config: Mapping[str, Any], incremental: bool = False) -> None:
    """Configure a non-root logger from a dictionary."""
    logger = logging.getLogger(name)
    self.common_logger_config(logger, config, incremental)
    logger.disabled = False
    propagate = config.get("propagate", None)
    if propagate is not None:
      logger.propagate = propagate

  def configure_root(self, config: Mapping[str, Any], incremental: bool = False) -> None:
    """Configure a root logger from a dictionary."""
    root = logging.getLogger()
    self.common_logger_config(root, config, incremental)


def dict_config(config: Mapping[str, Any] | LoggingConfigModel) -> None:
  """Configure logging using a mapping or an already-validated `LoggingConfigModel`."""
  if isinstance(config, LoggingConfigModel):
    config = config.model_dump(by_alias=True, exclude_none=True)
  DictConfigurator(config).configure()


def json_config(source: str | Path | io.TextIOBase) -> None:
  """Configure logging from a JSON source (a path, a path string, or a text file-like object)."""
  if isinstance(source, (str, Path)) and not hasattr(source, "read"):
    text = Path(source).read_text(encoding="utf-8")
  else:
    text = cast("io.TextIOBase", source).read()
  d = orjson.loads(text)
  assert isinstance(d, dict)
  dict_config(d)


def toml_to_json(source: str | bytes | Path) -> str:
  """Translate a TOML source (path, path string, or raw TOML text/bytes) into a JSON string."""
  # Standard library imports
  import tomllib

  if isinstance(source, Path):
    data = tomllib.loads(source.read_text(encoding="utf-8"))
  elif isinstance(source, bytes):
    data = tomllib.loads(source.decode("utf-8"))
  elif Path(source).exists():
    data = tomllib.loads(Path(source).read_text(encoding="utf-8"))
  else:
    data = tomllib.loads(source)
  return orjson.dumps(data).decode("utf-8")


def toml_config(source: str | bytes | Path) -> None:
  """Parse a TOML source and apply it as a logging configuration."""
  # Standard library imports
  import tomllib

  if isinstance(source, Path):
    data = tomllib.loads(source.read_text(encoding="utf-8"))
  elif isinstance(source, bytes):
    data = tomllib.loads(source.decode("utf-8"))
  elif Path(source).exists():
    data = tomllib.loads(Path(source).read_text(encoding="utf-8"))
  else:
    data = tomllib.loads(source)
  dict_config(data)


def _load_yaml_text(text: str) -> Any:
  """
  Parse *text* as YAML, preferring the `py-yaml12` accelerator if it is
  installed, and falling back to `pyyaml` (with its fastest available
  loader) otherwise.
  """
  try:
    # Third party imports
    import yaml12

    return yaml12.parse_yaml(text)
  except ImportError:
    pass

  # Third party imports
  import yaml

  try:
    # Third party imports
    from yaml import CSafeLoader as _Loader
  except ImportError:
    # Third party imports
    from yaml import SafeLoader as _Loader

  return yaml.load(text, Loader=_Loader)


def yaml_to_json(source: str | bytes | Path) -> str:
  """Translate a YAML source (path, path string, or raw YAML text/bytes) into a JSON string."""
  if isinstance(source, Path):
    text = source.read_text(encoding="utf-8")
  elif isinstance(source, bytes):
    text = source.decode("utf-8")
  elif Path(source).exists():
    text = Path(source).read_text(encoding="utf-8")
  else:
    text = source
  return orjson.dumps(_load_yaml_text(text)).decode("utf-8")


def yaml_config(source: str | bytes | Path) -> None:
  """Parse a YAML source and apply it as a logging configuration."""
  if isinstance(source, Path):
    text = source.read_text(encoding="utf-8")
  elif isinstance(source, bytes):
    text = source.decode("utf-8")
  elif Path(source).exists():
    text = Path(source).read_text(encoding="utf-8")
  else:
    text = source
  dict_config(_load_yaml_text(text))


def _receive_length_prefixed_chunk(conn: socket.socket) -> bytes | None:
  """Read a 4-byte length prefix followed by that many bytes from a connection."""
  chunk = conn.recv(LENGTH_PREFIX_SIZE)
  if len(chunk) != LENGTH_PREFIX_SIZE:
    return None
  slen = struct.unpack(">L", chunk)[0]
  chunk = conn.recv(slen)
  while len(chunk) < slen:
    chunk = chunk + conn.recv(slen - len(chunk))
  return chunk


def _process_socket_config_chunk(chunk: bytes) -> None:
  """Decode a received config chunk as JSON and apply it via `dict_config`."""
  decoded_chunk = chunk.decode("utf-8")
  try:
    d = orjson.loads(decoded_chunk)
    assert isinstance(d, dict)
    dict_config(d)
  except Exception:
    traceback.print_exc()


class _ConfigStreamHandler(StreamRequestHandler):
  """
  Handler for a logging configuration request.

  It expects a completely new logging configuration in JSON form and uses
  `dict_config` to install it.
  """

  @override
  def handle(self) -> None:
    """
    Handle a request.

    Each request is expected to be a 4-byte length, packed using
    struct.pack(">L", n), followed by the JSON config payload.
    Uses `dict_config()` to do the grunt work.
    """
    try:
      conn = self.connection
      server = cast("_ConfigSocketReceiver", self.server)
      chunk = _receive_length_prefixed_chunk(conn)
      if chunk is not None:
        if server.verify is not None:
          chunk = server.verify(chunk)
        if chunk is not None:  # verified, can process
          _process_socket_config_chunk(chunk)
      if server.ready:
        server.ready.set()
    except OSError as e:
      if e.errno != RESET_ERROR:
        raise


class _ConfigSocketReceiver(ThreadingTCPServer):
  """
  A simple TCP socket-based logging config receiver.
  """

  allow_reuse_address = True
  allow_reuse_port = False

  def __init__(
    self,
    host: str = "localhost",
    port: int = DEFAULT_LOGGING_CONFIG_PORT,
    handler: type[StreamRequestHandler] | None = None,
    ready: threading.Event | None = None,
    verify: Callable[[bytes], bytes | None] | None = None,
  ) -> None:
    ThreadingTCPServer.__init__(self, (host, port), cast("type[StreamRequestHandler]", handler))
    with _logging_lock:
      self.abort = 0
    self.timeout = 1
    self.ready = ready
    self.verify = verify

  def serve_until_stopped(self) -> None:
    # Standard library imports
    import select

    abort = 0
    while not abort:
      rd, _wr, _ex = select.select([self.socket.fileno()], [], [], self.timeout)
      if rd:
        self.handle_request()
      with _logging_lock:
        abort = self.abort
    self.server_close()


class _ConfigListenerServer(threading.Thread):
  """Thread which runs a `_ConfigSocketReceiver` until `stop_listening()` is called."""

  def __init__(
    self,
    rcvr: type[_ConfigSocketReceiver],
    hdlr: type[_ConfigStreamHandler],
    port: int,
    verify: Callable[[bytes], bytes | None] | None,
  ) -> None:
    super().__init__()
    self.rcvr = rcvr
    self.hdlr = hdlr
    self.port = port
    self.verify = verify
    self.ready = threading.Event()

  @override
  def run(self) -> None:
    server = self.rcvr(port=self.port, handler=self.hdlr, ready=self.ready, verify=self.verify)
    if self.port == 0:
      self.port = server.server_address[1]
    self.ready.set()
    global _listener
    with _logging_lock:
      _listener = server
    server.serve_until_stopped()


def listen(port: int = DEFAULT_LOGGING_CONFIG_PORT, verify: Callable[[bytes], bytes | None] | None = None) -> _ConfigListenerServer:
  """
  Start up a socket server on the specified port, and listen for new
  configurations.

  These will be sent as UTF-8 encoded JSON, suitable for processing by
  `dict_config()`.
  Returns a Thread object on which you can call start() to start the server,
  and which you can join() when appropriate. To stop the server, call
  stop_listening().

  Use the ``verify`` argument to verify any bytes received across the wire
  from a client. If specified, it should be a callable which receives a
  single argument - the bytes of configuration data received across the
  network - and it should return either ``None``, to indicate that the
  passed in bytes could not be verified and should be discarded, or a
  byte string which is then passed to the configuration machinery as
  normal. Note that you can return transformed bytes, e.g. by decrypting
  the bytes passed in.
  """
  return _ConfigListenerServer(_ConfigSocketReceiver, _ConfigStreamHandler, port, verify)


def stop_listening() -> None:
  """
  Stop the listening server which was created with a call to listen().
  """
  global _listener
  with _logging_lock:
    if _listener:
      _listener.abort = 1
      _listener = None
