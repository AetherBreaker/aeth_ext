# Standard library imports
from abc import ABCMeta
from logging import getLogger
from typing import TYPE_CHECKING, override

# Third party imports
from aiologic import Lock
from pydantic._internal._model_construction import ModelMetaclass

if TYPE_CHECKING:
  # Standard library imports
  from typing import Any


logger = getLogger(__name__)


__all__ = [
  "SingletonType",
  "SingletonTypeABC",
  "SingletonTypeBaseModel",
]


class SingletonType(type):
  __shared_instance_lock__: Lock  # pyright: ignore[reportUninitializedInstanceVariable]

  def __new__(mcs, name: str, bases: tuple[type, ...], attrs: dict[str, object]):
    cls = super().__new__(mcs, name, bases, attrs)
    cls.__shared_instance_lock__ = Lock()
    return cls

  @override
  def __call__(cls, *args: Any, **kwargs: Any):
    with cls.__shared_instance_lock__:
      try:
        return cls.__shared_instance__
      except AttributeError:
        cls.__shared_instance__ = super().__call__(*args, **kwargs)  # pyright: ignore[reportUninitializedInstanceVariable]
        return cls.__shared_instance__


class SingletonTypeABC(ABCMeta, SingletonType):
  pass


class SingletonTypeBaseModel(ModelMetaclass, SingletonType):
  pass
