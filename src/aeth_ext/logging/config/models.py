# Standard library imports
from typing import Annotated, Any, Literal

# Third party imports
from pydantic import BaseModel, ConfigDict, Field


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
  definition: Annotated[
    str | None,
    Field(
      description=(
        "Base64-encoded cloudpickle payload of a factory callable used to construct "
        "this formatter, taking precedence over `class_` and `()`. Only honoured "
        "when the `logging_allow_pickled_definitions` setting is enabled."
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
  definition: Annotated[
    str | None,
    Field(
      description=(
        "Base64-encoded cloudpickle payload of a factory callable used to construct "
        "this filter, taking precedence over `()`. Only honoured when the "
        "`logging_allow_pickled_definitions` setting is enabled."
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
  definition: Annotated[
    str | None,
    Field(
      description=(
        "Base64-encoded cloudpickle payload of a factory callable used to construct "
        "this handler, taking precedence over `class_` and `()`. Only honoured "
        "when the `logging_allow_pickled_definitions` setting is enabled."
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
    list[str] | str | None,
    Field(
      description=(
        "Names of entries in the top-level `filters` section to attach to this handler, "
        "or a converter-protocol string (e.g. `'runtime://...'`) resolving to such a list."
      )
    ),
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
    list[str] | str | None,
    Field(
      description=(
        "Names of entries in the top-level `filters` section to attach to this logger, "
        "or a converter-protocol string (e.g. `'runtime://...'`) resolving to such a list."
      )
    ),
  ] = None
  handlers: Annotated[
    list[str] | str | None,
    Field(
      description=(
        "Names of entries in the top-level `handlers` section to attach to this logger, "
        "or a converter-protocol string (e.g. `'runtime://...'`) resolving to such a list."
      )
    ),
  ] = None


class RootLoggerConfig(_LoggingConfigBaseModel):
  """Schema for the ``root`` section of a logging config."""

  model_config = ConfigDict(extra="forbid")

  merge_marker: Annotated[
    str | None,
    Field(
      alias="__merge__",
      description=(
        "Internal merge directive consumed by `merge_configs`. When set to `'deep'`, "
        "this entry is recursively field-merged into the corresponding base entry "
        "instead of replacing it wholesale. Stripped before the config is applied."
      ),
    ),
  ] = None
  level: Annotated[
    str | int | None,
    Field(description="The minimum log level (name such as `'WARNING'`, or numeric value) the root logger will process."),
  ] = None
  filters: Annotated[
    list[str] | str | None,
    Field(
      description=(
        "Names of entries in the top-level `filters` section to attach to the root logger, "
        "or a converter-protocol string (e.g. `'runtime://...'`) resolving to such a list."
      )
    ),
  ] = None
  handlers: Annotated[
    list[str] | str | None,
    Field(
      description=(
        "Names of entries in the top-level `handlers` section to attach to the root logger, "
        "or a converter-protocol string (e.g. `'runtime://...'`) resolving to such a list."
      )
    ),
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
