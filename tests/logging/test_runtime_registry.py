"""Tests for `aeth_ext.logging.config.runtime_registry`."""

# Third party imports
import pytest

# First party imports
from aeth_ext.logging.config import runtime_registry

_FIRST = "first-value"
_SECOND = "second-value"


class TestRegisterResolve:
  def test_register_then_resolve(self):
    sentinel = object()
    runtime_registry.register("thing", sentinel)
    assert runtime_registry.resolve("thing") is sentinel

  def test_register_overwrites(self):
    runtime_registry.register("thing", _FIRST)
    runtime_registry.register("thing", _SECOND)
    assert runtime_registry.resolve("thing") == _SECOND

  def test_resolve_missing_raises_value_error(self):
    with pytest.raises(ValueError, match="No runtime object registered under 'nope'"):
      runtime_registry.resolve("nope")

  def test_resolve_missing_message_mentions_register(self):
    with pytest.raises(ValueError, match=r"runtime_registry\.register"):
      runtime_registry.resolve("nope")


class TestUnregisterClear:
  def test_unregister_removes(self):
    runtime_registry.register("thing", 1)
    runtime_registry.unregister("thing")
    with pytest.raises(ValueError):
      runtime_registry.resolve("thing")

  def test_unregister_missing_is_noop(self):
    runtime_registry.unregister("never_registered")  # must not raise

  def test_clear_removes_everything(self):
    runtime_registry.register("a", 1)
    runtime_registry.register("b", 2)
    runtime_registry.clear()
    assert runtime_registry.registered_names() == frozenset()


class TestRegisteredNames:
  def test_snapshot_of_names(self):
    runtime_registry.clear()
    runtime_registry.register("a", 1)
    runtime_registry.register("b", 2)
    assert runtime_registry.registered_names() == {"a", "b"}

  def test_snapshot_is_independent(self):
    runtime_registry.clear()
    runtime_registry.register("a", 1)
    names = runtime_registry.registered_names()
    runtime_registry.register("b", 2)
    assert names == {"a"}


class TestTemporarilyRegistered:
  def test_registers_within_context(self):
    with runtime_registry.temporarily_registered(tmp_obj=_FIRST):
      assert runtime_registry.resolve("tmp_obj") == _FIRST
    with pytest.raises(ValueError):
      runtime_registry.resolve("tmp_obj")

  def test_restores_previous_value(self):
    runtime_registry.register("tmp_obj", "before")
    with runtime_registry.temporarily_registered(tmp_obj="during"):
      assert runtime_registry.resolve("tmp_obj") == "during"
    assert runtime_registry.resolve("tmp_obj") == "before"

  def test_restores_on_exception(self):
    runtime_registry.register("tmp_obj", "before")
    with pytest.raises(RuntimeError):
      with runtime_registry.temporarily_registered(tmp_obj="during"):
        raise RuntimeError("boom")
    assert runtime_registry.resolve("tmp_obj") == "before"

  def test_multiple_objects(self):
    with runtime_registry.temporarily_registered(one=_FIRST, two=_SECOND):
      assert runtime_registry.resolve("one") == _FIRST
      assert runtime_registry.resolve("two") == _SECOND
    assert "one" not in runtime_registry.registered_names()
    assert "two" not in runtime_registry.registered_names()
