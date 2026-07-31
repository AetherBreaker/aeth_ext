"""Tests for `aeth_ext.monkey_patcher.MonkeyPatcher`."""

# Standard library imports
import sys
from types import FunctionType
from typing import TYPE_CHECKING, override

# Third party imports
import pytest

# First party imports
from aeth_ext.monkey_patcher import MonkeyPatcher

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


class TestNotInstantiable:
  def test_calling_the_class_raises(self):
    with pytest.raises(TypeError):
      MonkeyPatcher()

  def test_calling_a_subclass_raises(self):
    class Patch(MonkeyPatcher):
      pass

    with pytest.raises(TypeError):
      Patch()


class TestMethodsForcedStatic:
  def test_plainly_defined_method_is_callable_with_no_self(self):
    """Patch methods are meant to be authored with no `self`/`cls` parameter at
    all, so the coerced staticmethod can be called bare -- see the module
    docstring's "written without a self/cls parameter" note."""

    class Patch(MonkeyPatcher):
      def apply() -> str:  # pyright: ignore[reportSelfClsParameterName]
        return "applied"

    assert Patch.apply() == "applied"  # pyright: ignore[reportAttributeAccessIssue]

  def test_dunder_methods_are_left_untouched(self):
    class Patch(MonkeyPatcher):
      @override
      def __repr__(self) -> str:
        return "custom-repr"

    # A plain (unwrapped) function still requires `self` -- it was never forced static.
    assert isinstance(Patch.__dict__["__repr__"], FunctionType)

  def test_explicit_classmethod_keeps_its_binding(self):
    calls: list[str | None] = []

    class Patch(MonkeyPatcher):
      @classmethod
      @override
      def apply_monkey_patches(cls, caller_file: str | None = None) -> None:
        calls.append(caller_file)

    # Reserved method name: must remain usable as a classmethod, not be forced static.
    Patch.apply_monkey_patches(caller_file=__file__)

    assert calls == [__file__]


class TestSlotsInjection:
  def test_subclass_without_declared_slots_gets_empty_slots(self):
    class Patch(MonkeyPatcher):
      pass

    assert Patch.__slots__ == ()

  def test_subclass_with_declared_slots_keeps_them(self):
    class Patch(MonkeyPatcher):
      __slots__ = ("custom_attr",)  # pyright: ignore[reportUninitializedInstanceVariable]

    assert Patch.__slots__ == ("custom_attr",)


class TestAllAttrNamesSnapshot:
  def test_captures_attr_names_at_class_creation_time(self):
    class Patch(MonkeyPatcher):
      def apply(self) -> None:
        pass

    assert "apply" in Patch.__all_attr_names__

  def test_base_class_snapshot_does_not_include_subclass_methods(self):
    class Patch(MonkeyPatcher):
      def apply(self) -> None:
        pass

    assert "apply" in Patch.__all_attr_names__
    assert "apply" not in MonkeyPatcher.__all_attr_names__


class TestApplyMonkeyPatches:
  def test_only_calls_methods_absent_from_the_base_class(self, tmp_path: Path):
    """End-to-end: `apply_monkey_patches` discovers a real subclass on disk and
    calls only the method that isn't already present on the base class."""
    marker = tmp_path / "patch_applied.marker"
    app = _pkg(tmp_path / "patchapp")
    _write(
      app / "base_module.py",
      "from aeth_ext.monkey_patcher import MonkeyPatcher\n\n\nclass Base(MonkeyPatcher):\n  def already_on_base() -> None:\n    pass\n",
    )
    caller_file = _write(
      app / "patch_module.py",
      "from pathlib import Path\n"
      "from patchapp.base_module import Base\n\n\n"
      "class Patch(Base):\n"
      f"  def new_patch_method() -> None:\n    Path({str(marker)!r}).write_text('applied')\n",
    )

    sys.path.insert(0, str(tmp_path))
    try:
      # Third party imports
      from patchapp.base_module import Base  # pyright: ignore[reportMissingImports]

      Base.apply_monkey_patches(caller_file=str(caller_file))
    finally:
      sys.path.remove(str(tmp_path))
      for mod_name in list(sys.modules):
        if mod_name == "patchapp" or mod_name.startswith("patchapp."):
          del sys.modules[mod_name]

    assert marker.read_text() == "applied"
