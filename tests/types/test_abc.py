"""Tests for `aeth_ext.types.abc`'s singleton metaclasses."""

# Standard library imports
from abc import abstractmethod
from threading import Barrier, Thread
from typing import override

# Third party imports
import pytest
from pydantic import BaseModel

# First party imports
from aeth_ext.types import abc as abc_mod


class TestSingletonType:
  """`SingletonType.__call__` caches exactly one instance per class."""

  def test_repeated_construction_returns_same_instance(self) -> None:
    class Widget(metaclass=abc_mod.SingletonType):
      def __init__(self) -> None:
        self.value = object()

    first = Widget()
    second = Widget()

    assert first is second

  def test_construction_args_are_ignored_after_first_call(self) -> None:
    class Recorder(metaclass=abc_mod.SingletonType):
      def __init__(self, tag: str) -> None:
        self.tag = tag

    first = Recorder("initial")
    second = Recorder("ignored-on-second-call")

    assert first is second
    assert second.tag == "initial"

  def test_unrelated_classes_do_not_share_instances(self) -> None:
    class Alpha(metaclass=abc_mod.SingletonType):
      pass

    class Beta(metaclass=abc_mod.SingletonType):
      pass

    assert Alpha() is not Beta()

  def test_each_class_gets_its_own_lock(self) -> None:
    class Alpha(metaclass=abc_mod.SingletonType):
      pass

    class Beta(metaclass=abc_mod.SingletonType):
      pass

    assert Alpha.__shared_instance_lock__ is not Beta.__shared_instance_lock__

  def test_concurrent_construction_yields_a_single_instance(self) -> None:
    """Many threads racing `cls()` for the first time must still construct once."""
    init_count = 0
    n_threads = 16
    barrier = Barrier(n_threads)

    class Concurrent(metaclass=abc_mod.SingletonType):
      def __init__(self) -> None:
        nonlocal init_count
        init_count += 1

    results: list[Concurrent] = [None] * n_threads  # pyright: ignore[reportAssignmentType]

    def worker(idx: int) -> None:
      barrier.wait()
      results[idx] = Concurrent()

    threads = [Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    assert init_count == 1
    assert len({id(r) for r in results}) == 1


class TestSingletonTypeABC:
  """`SingletonTypeABC` combines ABC enforcement with singleton semantics."""

  def test_abstract_method_prevents_instantiation(self) -> None:
    class AbstractBase(metaclass=abc_mod.SingletonTypeABC):
      @abstractmethod
      def do_thing(self) -> None: ...

    with pytest.raises(TypeError):
      AbstractBase()  # pyright: ignore[reportAbstractUsage]

  def test_concrete_subclass_is_still_a_singleton(self) -> None:
    class AbstractBase(metaclass=abc_mod.SingletonTypeABC):
      @abstractmethod
      def do_thing(self) -> None: ...

    class Concrete(AbstractBase):
      @override
      def do_thing(self) -> None:
        pass

    assert Concrete() is Concrete()


class TestSingletonTypeBaseModel:
  """`SingletonTypeBaseModel` combines pydantic's metaclass with singleton semantics."""

  def test_repeated_validation_returns_same_instance(self) -> None:
    class Config(BaseModel, metaclass=abc_mod.SingletonTypeBaseModel):
      name: str = "default"

    first = Config()
    second = Config(name="different")

    assert first is second
    assert second.name == "default"

  def test_still_behaves_like_a_pydantic_model(self) -> None:
    class Config(BaseModel, metaclass=abc_mod.SingletonTypeBaseModel):
      name: str = "default"

    instance = Config()

    assert instance.model_dump() == {"name": "default"}
