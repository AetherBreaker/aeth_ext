# Standard library imports
import inspect
from collections.abc import Callable
from logging import getLogger
from typing import Any, overload

# Third party imports
from pydantic import BaseModel, create_model

# First party imports
from aeth_ext.command_server.protocol import CommandMeta

logger = getLogger(__name__)

__all__ = ["command"]

COMMAND_ATTR = "_aeth_command_meta"
PARAMS_MODEL_ATTR = "_aeth_command_params_model"


def _build_params_model(fn: Callable[..., Any]) -> type[BaseModel]:
  """Build a dynamic pydantic model from ``fn``'s signature (excluding ``self``).

  Every parameter must carry a type annotation. Defaults are preserved so
  optional parameters remain optional on the wire.
  """
  sig = inspect.signature(fn)
  fields: dict[str, Any] = {}
  for name, param in sig.parameters.items():
    if name == "self":
      continue
    if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
      raise TypeError(f"Command {fn.__qualname__!r} may not use *args/**kwargs parameters")
    if param.annotation is inspect.Parameter.empty:
      raise TypeError(f"Command {fn.__qualname__!r} parameter {name!r} must have a type annotation")
    default = ... if param.default is inspect.Parameter.empty else param.default
    fields[name] = (param.annotation, default)
  return create_model(f"{fn.__name__}_Params", **fields)


@overload
def command(fn: Callable[..., Any], /) -> Callable[..., Any]: ...


@overload
def command(*, name: str | None = None, description: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def command(fn: Callable[..., Any] | None = None, /, *, name: str | None = None, description: str | None = None) -> Callable[..., Any]:
  """Mark an async method on a :class:`CommandServerBase` subclass as a remotely invocable command.

  Usable bare (``@command``) or with options (``@command(description="...")``).

  The method's signature (excluding ``self``) defines the command's parameters;
  every parameter must be type-annotated so a pydantic validation model and a
  JSON schema can be generated for it. A non-``None`` return annotation marks
  the command as returning a value, which clients will await.
  """

  def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
    if not inspect.iscoroutinefunction(fn):
      raise TypeError(f"Command {fn.__qualname__!r} must be an async method")

    params_model = _build_params_model(fn)

    return_annotation = inspect.signature(fn).return_annotation
    returns_value = return_annotation not in (inspect.Signature.empty, None, type(None))

    meta = CommandMeta(
      name=name or fn.__name__,
      description=description or inspect.getdoc(fn) or "",
      params_schema=params_model.model_json_schema(),
      returns_value=returns_value,
    )
    setattr(fn, COMMAND_ATTR, meta)
    setattr(fn, PARAMS_MODEL_ATTR, params_model)
    return fn

  return decorate(fn) if fn is not None else decorate
