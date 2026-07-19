"""Tests for `aeth_ext.static_eval`'s caller-relative subclass and constant search.

These exercise the ancestry-walk primitives directly against real temporary
package trees (real `__init__.py`/`.py` files with real source text) rather
than the actual `aeth_ext` package layout, so each test controls its own
`caller_file`/`ceiling_dir` and never depends on where this test file itself
lives on disk. Nothing is ever imported -- `find_subclasses_local` and
`parse_and_grab_constants` are purely AST-based, so a qualified-name string is
used as the "base" wherever a live class would otherwise be required.
"""

# Standard library imports
from os.path import normcase
from pathlib import Path

# Third party imports
import pytest

# First party imports
from aeth_ext import static_eval as se


@pytest.fixture(autouse=True)
def _fresh_caches():
  """Every scanned file is assumed immutable for the process's lifetime.

  Each test uses its own unique `tmp_path`, so cache keys can never collide
  across tests -- this just keeps the "no stale state" intent explicit.
  """
  se.reset_subclass_caches()
  yield
  se.reset_subclass_caches()


def _pkg(directory: Path) -> Path:
  """Create `directory` (and parents) as a package (with `__init__.py`)."""
  directory.mkdir(parents=True, exist_ok=True)
  (directory / "__init__.py").write_text("")
  return directory


def _write(path: Path, content: str) -> Path:
  """Write `content` to `path`, creating parent directories as needed."""
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(content)
  return path


class TestFindSubclassesLocalAncestry:
  """`find_subclasses_local` only ever walks upward from the caller."""

  def test_sibling_directory_is_not_searched(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")
    sibling_dir = _pkg(app / "sibling")
    _write(
      sibling_dir / "sibling_module.py",
      "from app.base_module import Base\n\nclass SiblingSub(Base):\n  pass\n",
    )

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()

  def test_child_directory_is_not_searched(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    caller_file = _write(app / "caller_module.py", "")
    child_dir = _pkg(app / "child")
    _write(
      child_dir / "child_module.py",
      "from app.base_module import Base\n\nclass ChildSub(Base):\n  pass\n",
    )

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()

  def test_ancestor_directory_is_found(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n\nclass AncestorSub(Base):\n  pass\n")
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert [info.qualname for info in results] == ["app.base_module.AncestorSub"]
    assert results[0].locality == 1

  def test_ceiling_is_not_exceeded(self, tmp_path: Path):
    """A class defined *above* the ceiling must never be discovered."""
    outside = _pkg(tmp_path / "outside")
    _write(outside / "base_module.py", "class Base:\n  pass\n\nclass OutsideSub(Base):\n  pass\n")
    app = _pkg(outside / "app")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("outside.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()


class TestFindSubclassesLocalPriority:
  """Locality is the primary sort key; inheritance depth only breaks ties."""

  def test_locality_beats_depth(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    _write(
      app / "deep_chain.py",
      "from app.base_module import Base\n\nclass DeepMid(Base):\n  pass\n\nclass DeepLeaf(DeepMid):\n  pass\n",
    )
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")
    _write(caller_dir / "near_module.py", "from app.base_module import Base\n\nclass NearSub(Base):\n  pass\n")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    # NearSub (locality 0) must rank ahead of both app-level (locality 1)
    # classes, regardless of its shallower inheritance depth.
    assert results[0].qualname == "app.near.near_module.NearSub"
    assert results[0].locality == 0
    assert {info.qualname for info in results[1:]} == {"app.deep_chain.DeepMid", "app.deep_chain.DeepLeaf"}
    assert all(info.locality == 1 for info in results[1:])

  def test_depth_is_tiebreaker_within_same_locality(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "chain.py", "class Base:\n  pass\n\nclass Mid(Base):\n  pass\n\nclass Leaf(Mid):\n  pass\n")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("app.chain.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    # Same locality (0) for both -- deeper/more-derived wins the tiebreak, so
    # index [0] is always "the most locally-defined, most-derived" match.
    assert [info.qualname for info in results] == ["app.chain.Leaf", "app.chain.Mid"]

  def test_recursive_false_limits_to_immediate_subclasses(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "chain.py", "class Base:\n  pass\n\nclass Mid(Base):\n  pass\n\nclass Leaf(Mid):\n  pass\n")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("app.chain.Base", caller_file=str(caller_file), ceiling_dir=str(app), recursive=False)

    assert [info.qualname for info in results] == ["app.chain.Mid"]


class TestFindSubclassesLocalIncludeNameFallback:
  def test_unresolvable_import_only_matches_with_fallback(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    _write(
      app / "mystery_module.py",
      # Base is referenced but never actually imported -- AST resolution
      # cannot determine what it points to, so only the bare-name fallback
      # can match this subclass.
      "class MysterySub(Base):\n  pass\n",
    )
    caller_file = _write(app / "caller_module.py", "")

    without_fallback = se.find_subclasses_local(
      "app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app), include_name_fallback=False
    )
    with_fallback = se.find_subclasses_local(
      "app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app), include_name_fallback=True
    )

    assert without_fallback == ()
    assert [info.qualname for info in with_fallback] == ["app.mystery_module.MysterySub"]


class TestSkipSubclassSearch:
  def test_non_ceiling_directory_can_skip_itself(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    mid = _pkg(app / "mid")
    _write(mid / "__init__.py", "SKIP_SUBCLASS_SEARCH = True\n")
    _write(mid / "mid_module.py", "from app.base_module import Base\n\nclass MidSub(Base):\n  pass\n")
    caller_dir = _pkg(mid / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()

  def test_ceiling_directory_ignores_its_own_skip_flag(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", "SKIP_SUBCLASS_SEARCH = True\n")
    _write(app / "base_module.py", "class Base:\n  pass\n\nclass CeilingSub(Base):\n  pass\n")
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert [info.qualname for info in results] == ["app.base_module.CeilingSub"]
    assert results[0].locality == 1


class TestCollectAncestryConfigFiles:
  """`_collect_ancestry_config_files` orders candidates farthest-first."""

  def test_orders_farthest_first_caller_last(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    files = se._collect_ancestry_config_files(str(caller_file), str(app))  # pyright: ignore[reportPrivateUsage]

    assert files == [
      app / "__main__.py",
      app / "__init__.py",
      caller_dir / "__main__.py",
      caller_dir / "__init__.py",
    ]

  def test_non_ceiling_can_skip_itself(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    mid = _pkg(app / "mid")
    _write(mid / "__init__.py", "SKIP_CONSTANT_SEARCH = True\n")
    caller_dir = _pkg(mid / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    files = se._collect_ancestry_config_files(str(caller_file), str(app))  # pyright: ignore[reportPrivateUsage]

    assert (mid / "__init__.py") not in files
    assert (mid / "__main__.py") not in files

  def test_ceiling_ignores_its_own_skip_flag(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", "SKIP_CONSTANT_SEARCH = True\n")
    caller_file = _write(app / "caller_module.py", "")

    files = se._collect_ancestry_config_files(str(caller_file), str(app))  # pyright: ignore[reportPrivateUsage]

    assert (app / "__init__.py") in files

  def test_disjoint_ceiling_is_still_unioned_in_at_lowest_priority(self, tmp_path: Path):
    """A caller whose own ancestry never reaches the ceiling (a sibling
    subtree, not an ancestor -- e.g. a shared library module reused by
    several applications) still gets the ceiling's own files unioned in, at
    the lowest priority, so application-level constants remain discoverable.
    """
    top = _pkg(tmp_path / "top")
    shared_lib = _pkg(top / "shared_lib")
    caller_file = _write(shared_lib / "caller_module.py", "")
    entrypoint_app = _pkg(top / "entrypoint_app")

    files = se._collect_ancestry_config_files(  # pyright: ignore[reportPrivateUsage]
      str(caller_file), str(entrypoint_app)
    )

    assert files[0] == entrypoint_app / "__main__.py"
    assert files[1] == entrypoint_app / "__init__.py"
    assert (shared_lib / "__init__.py") in files


class TestParseAndGrabConstantsEndToEnd:
  def test_finds_constant_defined_only_at_disjoint_entrypoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression test: a caller living in a sibling subtree of the true
    process entrypoint (e.g. a shared library module) must still discover
    constants defined only in the entrypoint's own `__main__.py`.
    """
    top = _pkg(tmp_path / "top")
    shared_lib = _pkg(top / "shared_lib")
    caller_file = _write(shared_lib / "caller_module.py", "")
    entrypoint_app = _pkg(top / "entrypoint_app")
    _write(entrypoint_app / "__main__.py", 'PROJECT_NAME = "entrypoint-value"\n')
    monkeypatch.setattr(se, "get_entrypoint_root", lambda: str(entrypoint_app))

    result = se.parse_and_grab_constants({"PROJECT_NAME": "project_name"}, caller_file=str(caller_file))

    assert result == {"project_name": "entrypoint-value"}

  def test_prefers_closer_definition_over_farther(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", 'PROJECT_NAME = "far-value"\n')
    caller_dir = _pkg(app / "near")
    _write(caller_dir / "__init__.py", 'PROJECT_NAME = "near-value"\n')
    caller_file = _write(caller_dir / "caller_module.py", "")
    monkeypatch.setattr(se, "get_entrypoint_root", lambda: str(app))

    result = se.parse_and_grab_constants({"PROJECT_NAME": "project_name"}, caller_file=str(caller_file))

    assert result == {"project_name": "near-value"}

  def test_unions_distinct_constants_across_levels(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", "TESTING = True\n")
    caller_dir = _pkg(app / "near")
    _write(caller_dir / "__init__.py", 'PROJECT_NAME = "near-value"\n')
    caller_file = _write(caller_dir / "caller_module.py", "")
    monkeypatch.setattr(se, "get_entrypoint_root", lambda: str(app))

    result = se.parse_and_grab_constants({"PROJECT_NAME": "project_name", "TESTING": "testing"}, caller_file=str(caller_file))

    assert result == {"project_name": "near-value", "testing": True}


class TestGetPackageRoot:
  def test_climbs_through_init_files(self, tmp_path: Path):
    app = _pkg(tmp_path / "app")
    sub = _pkg(app / "sub")
    mod = _write(sub / "mod.py", "")

    assert se.get_package_root(str(mod)) == str(app)

  def test_standalone_non_package_script_returns_own_directory(self, tmp_path: Path):
    standalone_dir = tmp_path / "scripts"
    standalone_dir.mkdir()
    mod = _write(standalone_dir / "mod.py", "")  # no __init__.py anywhere

    assert se.get_package_root(str(mod)) == str(standalone_dir)

  def test_site_packages_scopes_to_top_level_package(self, tmp_path: Path):
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    pkg = _pkg(site_packages / "mypkg")
    sub = _pkg(pkg / "sub")
    mod = _write(sub / "mod.py", "")

    assert se.get_package_root(str(mod)) == str(pkg)


class TestGetEntrypointRoot:
  def test_climbs_to_directory_with_main_file(self, tmp_path: Path):
    top = _pkg(tmp_path / "top")
    _write(top / "__main__.py", "")
    sub = _pkg(top / "sub")
    entry = _write(sub / "__main__.py", "")

    assert se.get_entrypoint_root(str(entry)) == str(sub)

  def test_skip_entrypoint_marker_lets_it_keep_climbing(self, tmp_path: Path):
    top = _pkg(tmp_path / "top")
    _write(top / "__main__.py", "")
    sub = _pkg(top / "sub")
    _write(sub / "__init__.py", "SKIP_ENTRYPOINT_MARKER = True\n")
    entry = _write(sub / "__main__.py", "")

    assert se.get_entrypoint_root(str(entry)) == str(top)


class TestGetCallerFile:
  def test_depth_zero_returns_direct_caller(self):
    assert normcase(se.get_caller_file(0) or "") == normcase(str(Path(__file__).resolve()))

  def test_excessive_depth_returns_none(self):
    assert se.get_caller_file(10_000) is None
