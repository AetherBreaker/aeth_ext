# Standard library imports
from abc import ABCMeta
from logging import getLogger

# Third party imports
from aiologic import Lock

logger = getLogger(__name__)


class SingletonType(type):
  __shared_instance_lock__: Lock

  def __new__(mcs, name: str, bases: tuple[type, ...], attrs: dict[str, object]):
    cls = super().__new__(mcs, name, bases, attrs)
    cls.__shared_instance_lock__ = Lock()
    return cls

  def __call__(cls, *args, **kwargs):
    with cls.__shared_instance_lock__:
      try:
        return cls.__shared_instance__
      except AttributeError:
        cls.__shared_instance__ = super().__call__(*args, **kwargs)
        return cls.__shared_instance__


class SingletonTypeABC(ABCMeta, SingletonType):
  pass
