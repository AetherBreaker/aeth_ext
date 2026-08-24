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
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext import static_eval as se

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator


@pytest.fixture(autouse=True)
def _fresh_caches() -> Generator[None]:
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

  def test_sibling_directory_is_not_searched(self, tmp_path: Path) -> None:
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

  def test_child_directory_is_not_searched(self, tmp_path: Path) -> None:
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

  def test_ancestor_directory_is_found(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n\nclass AncestorSub(Base):\n  pass\n")
    caller_dir = _pkg(app / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert [info.qualname for info in results] == ["app.base_module.AncestorSub"]
    assert results[0].locality == 1

  def test_ceiling_is_not_exceeded(self, tmp_path: Path) -> None:
    """A class defined *above* the ceiling must never be discovered."""
    outside = _pkg(tmp_path / "outside")
    _write(outside / "base_module.py", "class Base:\n  pass\n\nclass OutsideSub(Base):\n  pass\n")
    app = _pkg(outside / "app")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("outside.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()


class TestFindSubclassesLocalPriority:
  """Locality is the primary sort key; inheritance depth only breaks ties."""

  def test_locality_beats_depth(self, tmp_path: Path) -> None:
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

  def test_depth_is_tiebreaker_within_same_locality(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "chain.py", "class Base:\n  pass\n\nclass Mid(Base):\n  pass\n\nclass Leaf(Mid):\n  pass\n")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("app.chain.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    # Same locality (0) for both -- deeper/more-derived wins the tiebreak, so
    # index [0] is always "the most locally-defined, most-derived" match.
    assert [info.qualname for info in results] == ["app.chain.Leaf", "app.chain.Mid"]

  def test_recursive_false_limits_to_immediate_subclasses(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "chain.py", "class Base:\n  pass\n\nclass Mid(Base):\n  pass\n\nclass Leaf(Mid):\n  pass\n")
    caller_file = _write(app / "caller_module.py", "")

    results = se.find_subclasses_local("app.chain.Base", caller_file=str(caller_file), ceiling_dir=str(app), recursive=False)

    assert [info.qualname for info in results] == ["app.chain.Mid"]


class TestFindSubclassesLocalIncludeNameFallback:
  def test_unresolvable_import_only_matches_with_fallback(self, tmp_path: Path) -> None:
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
  def test_non_ceiling_directory_can_skip_itself(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "base_module.py", "class Base:\n  pass\n")
    mid = _pkg(app / "mid")
    _write(mid / "__init__.py", "SKIP_SUBCLASS_SEARCH = True\n")
    _write(mid / "mid_module.py", "from app.base_module import Base\n\nclass MidSub(Base):\n  pass\n")
    caller_dir = _pkg(mid / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    results = se.find_subclasses_local("app.base_module.Base", caller_file=str(caller_file), ceiling_dir=str(app))

    assert results == ()

  def test_ceiling_directory_ignores_its_own_skip_flag(self, tmp_path: Path) -> None:
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

  def test_orders_farthest_first_caller_last(self, tmp_path: Path) -> None:
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

  def test_non_ceiling_can_skip_itself(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    mid = _pkg(app / "mid")
    _write(mid / "__init__.py", "SKIP_CONSTANT_SEARCH = True\n")
    caller_dir = _pkg(mid / "near")
    caller_file = _write(caller_dir / "caller_module.py", "")

    files = se._collect_ancestry_config_files(str(caller_file), str(app))  # pyright: ignore[reportPrivateUsage]

    assert (mid / "__init__.py") not in files
    assert (mid / "__main__.py") not in files

  def test_ceiling_ignores_its_own_skip_flag(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", "SKIP_CONSTANT_SEARCH = True\n")
    caller_file = _write(app / "caller_module.py", "")

    files = se._collect_ancestry_config_files(str(caller_file), str(app))  # pyright: ignore[reportPrivateUsage]

    assert (app / "__init__.py") in files

  def test_disjoint_ceiling_is_still_unioned_in_at_lowest_priority(self, tmp_path: Path) -> None:
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
  def test_finds_constant_defined_only_at_disjoint_entrypoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

  def test_prefers_closer_definition_over_farther(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", 'PROJECT_NAME = "far-value"\n')
    caller_dir = _pkg(app / "near")
    _write(caller_dir / "__init__.py", 'PROJECT_NAME = "near-value"\n')
    caller_file = _write(caller_dir / "caller_module.py", "")
    monkeypatch.setattr(se, "get_entrypoint_root", lambda: str(app))

    result = se.parse_and_grab_constants({"PROJECT_NAME": "project_name"}, caller_file=str(caller_file))

    assert result == {"project_name": "near-value"}

  def test_unions_distinct_constants_across_levels(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _pkg(tmp_path / "app")
    _write(app / "__init__.py", "TESTING = True\n")
    caller_dir = _pkg(app / "near")
    _write(caller_dir / "__init__.py", 'PROJECT_NAME = "near-value"\n')
    caller_file = _write(caller_dir / "caller_module.py", "")
    monkeypatch.setattr(se, "get_entrypoint_root", lambda: str(app))

    result = se.parse_and_grab_constants({"PROJECT_NAME": "project_name", "TESTING": "testing"}, caller_file=str(caller_file))

    assert result == {"project_name": "near-value", "testing": True}


class TestGetPackageRoot:
  def test_climbs_through_init_files(self, tmp_path: Path) -> None:
    app = _pkg(tmp_path / "app")
    sub = _pkg(app / "sub")
    mod = _write(sub / "mod.py", "")

    assert se.get_package_root(str(mod)) == str(app)

  def test_standalone_non_package_script_returns_own_directory(self, tmp_path: Path) -> None:
    standalone_dir = tmp_path / "scripts"
    standalone_dir.mkdir()
    mod = _write(standalone_dir / "mod.py", "")  # no __init__.py anywhere

    assert se.get_package_root(str(mod)) == str(standalone_dir)

  def test_site_packages_scopes_to_top_level_package(self, tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    pkg = _pkg(site_packages / "mypkg")
    sub = _pkg(pkg / "sub")
    mod = _write(sub / "mod.py", "")

    assert se.get_package_root(str(mod)) == str(pkg)

  def test_site_packages_namespace_package_scopes_to_its_own_top_level_directory(self, tmp_path: Path) -> None:
    """A PEP 420 namespace package (no `__init__.py` anywhere) breaks `_module_qualname`'s
    `__init__.py`-climbing, which would otherwise silently fall back to just the anchor file's own
    basename instead of the real top-level package name -- the top-level name must come straight
    off the anchor path's own site-packages-relative segment instead (D-copilot regression)."""
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    ns_pkg = site_packages / "ns_pkg"  # deliberately no __init__.py -- a namespace package
    mod = _write(ns_pkg / "app.py", "")

    assert se.get_package_root(str(mod)) == str(ns_pkg)

  def test_site_packages_top_level_module_file_strips_its_own_extension(self, tmp_path: Path) -> None:
    """A single-file top-level module directly under `site-packages` (not inside any package
    subdirectory, e.g. `six.py`) must resolve to its own name without the `.py` extension, not the
    literal filename."""
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    mod = _write(site_packages / "six.py", "")

    assert se.get_package_root(str(mod)) == str(site_packages / "six")

  def test_site_packages_top_level_compiled_extension_strips_full_platform_suffix(self, tmp_path: Path) -> None:
    """A compiled extension's real suffix (e.g. ".cpython-314-x86_64-linux-gnu.so") has multiple
    dot-segments -- a plain splitext() only strips the last one, leaving the ABI/platform tag
    attached to the "package" name instead of the real importable module name (D-copilot
    regression)."""
    # Standard library imports
    from importlib.machinery import EXTENSION_SUFFIXES

    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    suffix = max(EXTENSION_SUFFIXES, key=len)
    mod = _write(site_packages / f"_cffi_backend{suffix}", "")

    assert se.get_package_root(str(mod)) == str(site_packages / "_cffi_backend")

  def test_dist_packages_nested_namespace_package_scopes_to_its_own_top_level_directory(self, tmp_path: Path) -> None:
    """Debian/Ubuntu system Python uses `dist-packages` instead of `site-packages`, which the
    generic (non-install-dir) climb doesn't recognize as special -- for a namespace package nested
    more than one level deep (no `__init__.py` at any level, so nothing to climb through), that
    generic climb stops one level too early, at the file's immediate directory rather than the
    real top-level package name (D-copilot regression)."""
    dist_packages = tmp_path / "usr" / "lib" / "python3" / "dist-packages"
    ns_pkg = dist_packages / "ns_pkg"  # deliberately no __init__.py anywhere -- a namespace package
    mod = _write(ns_pkg / "sub" / "mod.py", "")

    assert se.get_package_root(str(mod)) == str(ns_pkg)


class TestGetEntrypointRoot:
  def test_climbs_to_directory_with_main_file(self, tmp_path: Path) -> None:
    top = _pkg(tmp_path / "top")
    _write(top / "__main__.py", "")
    sub = _pkg(top / "sub")
    entry = _write(sub / "__main__.py", "")

    assert se.get_entrypoint_root(str(entry)) == str(sub)

  def test_skip_entrypoint_marker_lets_it_keep_climbing(self, tmp_path: Path) -> None:
    top = _pkg(tmp_path / "top")
    _write(top / "__main__.py", "")
    sub = _pkg(top / "sub")
    _write(sub / "__init__.py", "SKIP_ENTRYPOINT_MARKER = True\n")
    entry = _write(sub / "__main__.py", "")

    assert se.get_entrypoint_root(str(entry)) == str(top)


class TestGetEntrypointRootConsoleScriptRedirect:
  def test_redirects_to_the_real_target_module_of_an_installed_console_script(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """A modern installer (`uv`, recent `pip`) generates console-script wrappers as
    self-contained, zipapp-style executables: the wrapper's own
    `sys.modules["__main__"].__file__` is a *virtual* path inside it (e.g.
    `mytool.exe/__main__.py`, where `mytool.exe` is a real file) that never
    corresponds to a real directory -- the plain package-climb can't reach the
    real application code from it, so it must be redirected via the wrapper's
    registered `console_scripts` entry point first (D-copilot regression)."""
    app_pkg = _pkg(tmp_path / "myapp")
    cli_file = _write(app_pkg / "cli.py", "")

    wrapper_exe = tmp_path / "Scripts" / "mytool.exe"
    wrapper_exe.parent.mkdir(parents=True)
    wrapper_exe.write_bytes(b"")
    virtual_main_file = str(wrapper_exe / "__main__.py")

    monkeypatch.setattr(se, "argv", [str(wrapper_exe)])
    monkeypatch.setattr(se, "get_path", lambda *_args, **_kwargs: str(wrapper_exe.parent))
    fake_entry_point = type("FakeEntryPoint", (), {"name": "mytool", "value": "myapp.cli:main"})()
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [fake_entry_point])
    fake_spec = type("FakeSpec", (), {"origin": str(cli_file)})()
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: fake_spec)

    root = se.get_entrypoint_root(virtual_main_file)

    assert root == str(app_pkg)

  def test_redirects_a_real_posix_wrapper_script_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX installers (`uv`, `pip`) generate console-script wrappers as plain, real shebang
    scripts rather than the zipapp-style virtual paths Windows installers produce -- `main_file` is
    a real, independently-existing file that just happens to live in the venv's `bin/` instead of
    the application's own package tree. The redirect must not gate on "is this a virtual path": a
    real wrapper file needs the same redirect for the same reason (D-copilot regression)."""
    app_pkg = _pkg(tmp_path / "myapp")
    cli_file = _write(app_pkg / "cli.py", "")

    wrapper_script = tmp_path / "bin" / "mytool"
    wrapper_script.parent.mkdir(parents=True)
    wrapper_script.write_text("#!/usr/bin/env python\n")

    monkeypatch.setattr(se, "argv", [str(wrapper_script)])
    monkeypatch.setattr(se, "get_path", lambda *_args, **_kwargs: str(wrapper_script.parent))
    fake_entry_point = type("FakeEntryPoint", (), {"name": "mytool", "value": "myapp.cli:main"})()
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [fake_entry_point])
    fake_spec = type("FakeSpec", (), {"origin": str(cli_file)})()
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: fake_spec)

    root = se.get_entrypoint_root(str(wrapper_script))

    assert root == str(app_pkg)

  def test_redirects_a_wrapper_invoked_through_a_pipx_style_symlink(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """pipx (and similar deployment layouts) invoke the wrapper through a symlink living outside
    the venv's own scripts directory entirely -- `argv[0]`/`main_file` name the symlink, not its
    real venv location, so the redirect must resolve it before comparing against the scripts
    directory rather than rejecting a real wrapper based on where its symlink happens to live
    (D-copilot regression)."""
    app_pkg = _pkg(tmp_path / "myapp")
    cli_file = _write(app_pkg / "cli.py", "")

    real_wrapper = tmp_path / "venv-bin" / "mytool"
    real_wrapper.parent.mkdir(parents=True)
    real_wrapper.write_text("#!/usr/bin/env python\n")

    symlink_dir = tmp_path / "local-bin"
    symlink_dir.mkdir()
    symlinked_wrapper = symlink_dir / "mytool"
    try:
      symlinked_wrapper.symlink_to(real_wrapper)
    except OSError:
      pytest.skip("symlinks not supported in this environment")

    monkeypatch.setattr(se, "argv", [str(symlinked_wrapper)])
    monkeypatch.setattr(se, "get_path", lambda *_args, **_kwargs: str(real_wrapper.parent))
    fake_entry_point = type("FakeEntryPoint", (), {"name": "mytool", "value": "myapp.cli:main"})()
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [fake_entry_point])
    fake_spec = type("FakeSpec", (), {"origin": str(cli_file)})()
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: fake_spec)

    root = se.get_entrypoint_root(str(symlinked_wrapper))

    assert root == str(app_pkg)

  def test_a_virtual_path_with_no_matching_entry_point_falls_back_unchanged(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """No matching `console_scripts` entry point (e.g. some other zipapp not
    installed as a console script) leaves `main_file` untouched -- no worse than
    before this redirect existed, not a new failure mode."""
    wrapper_exe = tmp_path / "Scripts" / "mytool.exe"
    wrapper_exe.parent.mkdir(parents=True)
    wrapper_exe.write_bytes(b"")
    virtual_main_file = str(wrapper_exe / "__main__.py")

    monkeypatch.setattr(se, "argv", [str(wrapper_exe)])
    monkeypatch.setattr(se, "get_path", lambda *_args, **_kwargs: str(wrapper_exe.parent))
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [])

    root = se.get_entrypoint_root(virtual_main_file)

    assert root == str(wrapper_exe)

  def test_an_ordinary_script_sharing_a_console_scripts_basename_is_not_redirected(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """A real, ordinary app script that merely happens to share a basename with some unrelated
    installed package's registered console command must not be redirected to that package's
    entrypoint just because it's a real file matching `argv[0]` -- only a file actually sitting in
    the interpreter's own installed-scripts directory is a genuine wrapper (D-copilot regression)."""
    app_pkg = _pkg(tmp_path / "myapp")
    own_script = app_pkg / "mytool.py"
    own_script.write_text("")

    monkeypatch.setattr(se, "argv", [str(own_script)])
    monkeypatch.setattr(se, "get_path", lambda *_args, **_kwargs: str(tmp_path / "Scripts"))
    fake_entry_point = type("FakeEntryPoint", (), {"name": "mytool", "value": "unrelated.cli:main"})()
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [fake_entry_point])

    root = se.get_entrypoint_root(str(own_script))

    assert root == str(app_pkg)


class TestGetCallerFile:
  def test_depth_zero_returns_direct_caller(self) -> None:
    assert normcase(se.get_caller_file(0) or "") == normcase(str(Path(__file__).resolve()))

  def test_excessive_depth_returns_none(self) -> None:
    assert se.get_caller_file(10_000) is None
