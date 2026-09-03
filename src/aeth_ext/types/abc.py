"""Singleton metaclasses, including variants compatible with ``ABCMeta`` and pydantic's ``ModelMetaclass``."""

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


__all__ = ["SingletonType", "SingletonTypeABC", "SingletonTypeBaseModel"]


class SingletonType(type):
  """Metaclass that returns one shared instance per class, created under a per-class lock."""

  __shared_instance_lock__: Lock  # pyright: ignore[reportUninitializedInstanceVariable]

  def __new__(mcs, name: str, bases: tuple[type, ...], attrs: dict[str, object]):  # noqa: ANN204
    """Create the class and give it its own ``__shared_instance_lock__``."""
    cls = super().__new__(mcs, name, bases, attrs)
    cls.__shared_instance_lock__ = Lock()
    return cls

  @override
  def __call__(cls, *args: Any, **kwargs: Any) -> Any:
    with cls.__shared_instance_lock__:
      try:
        return cls.__shared_instance__
      except AttributeError:
        cls.__shared_instance__ = super().__call__(*args, **kwargs)  # pyright: ignore[reportUninitializedInstanceVariable]
        return cls.__shared_instance__


class SingletonTypeABC(ABCMeta, SingletonType):
  """``SingletonType`` combined with ``ABCMeta`` for abstract singleton classes."""


class SingletonTypeBaseModel(ModelMetaclass, SingletonType):
  """``SingletonType`` combined with pydantic's ``ModelMetaclass`` for singleton models."""
