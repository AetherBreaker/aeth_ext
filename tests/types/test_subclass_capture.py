"""Tests for `aeth_ext.types.subclass_capture.CapturesSubclasses`."""

# Standard library imports
import sys
from typing import TYPE_CHECKING

# Third party imports
from pydantic import BaseModel

# First party imports
from aeth_ext.types.subclass_capture import CapturesSubclasses

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


def _pkg(directory: Path) -> Path:
  directory.mkdir(parents=True, exist_ok=True)
  (directory / "__init__.py").write_text("")
  return directory


def _write(path: Path, content: str) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content)
  return path


class TestInstanceRegistry:
  """Each root subclass of `CapturesSubclasses` gets its own instance registry."""

  def test_root_subclass_starts_with_an_empty_registry(self) -> None:
    class Root(CapturesSubclasses):
      pass

    assert Root.__instances__ == []

  def test_unrelated_roots_do_not_share_a_registry(self) -> None:
    class RootA(CapturesSubclasses):
      pass

    class RootB(CapturesSubclasses):
      pass

    RootA()

    assert RootA.__instances__ != []
    assert RootB.__instances__ == []

  def test_deep_subclass_registers_into_its_roots_registry(self) -> None:
    class Root(CapturesSubclasses):
      pass

    class Deep(Root):
      pass

    instance = Deep()

    assert instance in Root.__instances__
    assert Root.__instances__ is Deep.__instances__


class TestPostInitHook:
  """`__post_init__` fires exactly once per instance, however deep the `__init__` chain."""

  def test_fires_on_plain_init(self) -> None:
    calls: list[object] = []

    class Root(CapturesSubclasses):
      def __post_init__(self) -> None:
        super().__post_init__()
        calls.append(self)

    instance = Root()

    assert calls == [instance]

  def test_fires_once_through_a_super_init_chain(self) -> None:
    calls: list[object] = []

    class Root(CapturesSubclasses):
      def __post_init__(self) -> None:
        super().__post_init__()
        calls.append(self)

    class Middle(Root):
      def __init__(self) -> None:
        super().__init__()

    class Leaf(Middle):
      def __init__(self) -> None:
        super().__init__()

    instance = Leaf()

    assert calls == [instance]

  def test_fires_via_pydantic_model_validate(self) -> None:
    class Root(BaseModel, CapturesSubclasses):
      name: str = "default"
      seen: list[object] = []

      def __post_init__(self) -> None:
        super().__post_init__()
        self.seen.append(self)

    instance = Root.model_validate({"name": "from-validate"})

    assert instance in Root.__instances__


class TestFinalClsResolution:
  """`get_final_cls`/`get_final_model` prefer the most recently created matching instance."""

  def test_returns_most_recent_matching_instance(self) -> None:
    class Root(CapturesSubclasses):
      pass

    class SubA(Root):
      pass

    class SubB(Root):
      pass

    SubA()
    latest = SubB()

    assert Root.get_final_cls(caller_file=__file__) is latest

  def test_ignores_instances_of_unrelated_classes(self) -> None:
    """With no matching instance, `get_final_cls` falls back to constructing a
    fresh instance of the deepest local subclass -- it never returns an
    instance belonging to an unrelated `CapturesSubclasses` hierarchy.
    """

    class RootA(CapturesSubclasses):
      pass

    class RootB(CapturesSubclasses):
      pass

    other_root_instance = RootB()

    result = RootA.get_final_cls(caller_file=__file__)

    assert isinstance(result, RootA)
    assert result is not other_root_instance


class TestSubclassDiscovery:
  """`get_deepest_subclass`/`get_all_subclasses` fall back to static file scanning."""

  def test_get_deepest_subclass_finds_local_definition(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "discoverapp")
    _write(
      app / "base_module.py",
      "from aeth_ext.types.subclass_capture import CapturesSubclasses\n\nclass Base(CapturesSubclasses):\n  pass\n",
    )
    caller_file = _write(
      app / "caller_module.py",
      "from discoverapp.base_module import Base\n\nclass Concrete(Base):\n  pass\n",
    )

    sys.path.insert(0, str(tmp_path))
    try:
      # Third party imports
      from discoverapp.base_module import Base  # pyright: ignore[reportMissingImports]

      deepest = Base.get_deepest_subclass(caller_file=str(caller_file))

      assert deepest.__name__ == "Concrete"
    finally:
      sys.path.remove(str(tmp_path))
      for mod_name in list(sys.modules):
        if mod_name == "discoverapp" or mod_name.startswith("discoverapp."):
          del sys.modules[mod_name]
