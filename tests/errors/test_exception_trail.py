"""Tests for `aeth_ext.errors.exception_trail`."""

# Standard library imports
import json
import re
from pathlib import Path

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import exception_trail as exception_trail_module
from aeth_ext.errors.exception_trail import (
  OriginCategory,
  TrailEntry,
  _compile_pattern,  # pyright: ignore[reportPrivateUsage]
  build_exception_trail,
)
from aeth_ext.static_eval import get_package_root


class TestOriginCategory:
  def test_members_use_their_own_name_as_value(self) -> None:
    assert OriginCategory.FIRST_PARTY.value == "FIRST_PARTY"
    assert OriginCategory.THIRD_PARTY.value == "THIRD_PARTY"
    assert OriginCategory.STDLIB.value == "STDLIB"
    assert OriginCategory.UNPACKAGED.value == "UNPACKAGED"


class TestTrailEntry:
  def test_fields_are_positional_and_named(self) -> None:
    entry = TrailEntry(module="pkg.mod", category=OriginCategory.FIRST_PARTY, file="/pkg/mod.py")
    assert entry.module == "pkg.mod"
    assert entry.category is OriginCategory.FIRST_PARTY
    assert entry.file == "/pkg/mod.py"


class TestCompilePatternLiteral:
  def test_exact_match(self) -> None:
    pattern = _compile_pattern("scheduled_invoice_processor.database")
    assert pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_as_prefix(self) -> None:
    """A bare literal must not match as a prefix of a longer dotted name."""
    pattern = _compile_pattern("database")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_as_suffix(self) -> None:
    """A bare literal must not match as a suffix either -- full anchoring both ends."""
    pattern = _compile_pattern("scheduled_invoice_processor")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")


class TestCompilePatternSingleStar:
  def test_matches_exactly_one_segment(self) -> None:
    pattern = _compile_pattern("scheduled_invoice_processor.*.database")
    assert pattern.fullmatch("scheduled_invoice_processor.suppliers.database")

  def test_rejects_zero_segments(self) -> None:
    pattern = _compile_pattern("scheduled_invoice_processor.*.database")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_two_segments(self) -> None:
    pattern = _compile_pattern("scheduled_invoice_processor.*.database")
    assert not pattern.fullmatch("scheduled_invoice_processor.a.b.database")


class TestCompilePatternDoubleStar:
  @pytest.mark.parametrize(
    "module",
    [
      "scheduled_invoice_processor.database",
      "scheduled_invoice_processor.suppliers.database",
      "scheduled_invoice_processor.a.b.database",
    ],
  )
  def test_matches_zero_one_and_multiple_segments(self, module: str) -> None:
    pattern = _compile_pattern("scheduled_invoice_processor.**.database")
    assert pattern.fullmatch(module)

  def test_matches_as_leading_wildcard(self) -> None:
    pattern = _compile_pattern("**.gspread.**")
    assert pattern.fullmatch("gspread")

  @pytest.mark.parametrize(
    "module",
    [
      "gspread.auth",
      "scheduled_invoice_processor.gspread",
      "scheduled_invoice_processor.gspread.utils",
      "a.b.gspread.c.d",
    ],
  )
  def test_matches_gspread_anywhere(self, module: str) -> None:
    pattern = _compile_pattern("**.gspread.**")
    assert pattern.fullmatch(module)

  def test_rejects_when_segment_absent(self) -> None:
    pattern = _compile_pattern("**.gspread.**")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")

  @pytest.mark.parametrize("module", ["a", "a.b", "a.b.c"])
  def test_consecutive_double_stars_are_collapsed_into_one(self, module: str) -> None:
    """`"**.**"` must behave exactly like a single `"**"` -- each already means "zero or more
    segments", so generating a separate regex piece per occurrence produces two half-open groups
    (one missing its leading dot, one its trailing dot) that jointly match nothing at all
    (D-copilot regression)."""
    pattern = _compile_pattern("**.**")
    assert pattern.fullmatch(module)


class TestCompilePatternAnchoring:
  def test_fully_anchored_not_findall_style(self) -> None:
    """A pattern that would match as an unanchored substring must not match here."""
    assert re.search("database", "scheduled_invoice_processor.database.orm") is not None  # sanity: substring exists
    assert not _compile_pattern("database").fullmatch("scheduled_invoice_processor.database.orm")


def _raise_stdlib_error() -> None:
  json.loads("{not valid json")


def _raise_directly() -> None:
  raise ValueError("boom")


def _wrap_and_raise() -> None:
  try:
    _raise_directly()
  except ValueError as e:
    raise RuntimeError("wrapped") from e


def _wrap_stdlib_error_and_raise() -> None:
  """Wraps a cause whose frames are STDLIB-categorized, unlike `_wrap_and_raise`'s (this test
  module's own frames), so chain-walking behavior is distinguishable by category rather than by
  entry count -- see `test_walk_chain_false_excludes_the_cause`."""
  try:
    json.loads("{not valid json")
  except json.JSONDecodeError as e:
    raise RuntimeError("wrapped") from e


def _raise_with_convergent_cause_and_context() -> None:
  """`__cause__` (`cause_exc`) and `__context__` (the `JSONDecodeError`) are two *different*
  objects, but not unrelated ones: the `JSONDecodeError` is raised while `cause_exc` is still the
  active exception, so `context.__context__ is cause_exc` -- fully ordered ancestry despite the
  identity difference. Must NOT be flagged ambiguous (see `_reachable_via_ancestry`)."""
  try:
    _raise_directly()
  except ValueError as cause_exc:
    try:
      json.loads("{not valid json")
    except json.JSONDecodeError:
      raise RuntimeError("convergent") from cause_exc


def _raise_with_divergent_cause_and_context() -> None:
  """`__cause__` (this module, via `_raise_directly`) and `__context__` (stdlib, via
  `json.loads`) come from two disjoint, non-nested `try` blocks -- neither is reachable from the
  other at all, the genuinely ambiguous-ancestry shape."""
  try:
    raise ValueError("boom")
  except ValueError as cause_exc:
    saved_cause = cause_exc

  try:
    json.loads("{not valid json")
  except json.JSONDecodeError:
    raise RuntimeError("ambiguous") from saved_cause


def _raise_nested_context_only_chain() -> None:
  """A pure `__context__` chain three deep, with no `__cause__` involved at any level: the
  outermost (`ValueError`, this module) is active when the middle (`json.JSONDecodeError`,
  stdlib) is raised, which is in turn active when the innermost (`RuntimeError`, this module) is
  raised -- oldest-first ordering should read this module, stdlib, this module."""
  try:
    raise ValueError("outer")
  except ValueError:
    try:
      json.loads("{not valid json")
    except json.JSONDecodeError:
      raise RuntimeError("innermost")  # noqa: B904 -- deliberate: implicit context only, no `from`


class TestBuildExceptionTrailConstruction:
  def test_entries_are_origin_first(self) -> None:
    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    # The innermost frame (where `raise` actually executed) is entries[0].
    assert trail.entries[0].module == __name__
    assert trail.origin == trail.entries[0]

  def test_a_stdlib_frame_in_the_call_path_is_categorized_stdlib(self) -> None:
    with pytest.raises(json.JSONDecodeError) as exc_info:
      _raise_stdlib_error()
    trail = build_exception_trail(exc_info.value)

    assert any(entry.category is OriginCategory.STDLIB for entry in trail.entries)

  def test_a_frozen_stdlib_frame_with_no_real_file_is_still_categorized_stdlib(self) -> None:
    """A frozen stdlib frame (e.g. `<frozen importlib._bootstrap>`) has a real stdlib module name
    but no real backing file -- simulated here the same way as the UNPACKAGED exec tests below,
    but with a genuine stdlib module name. `_categorize` must check the module name before
    falling back on a missing file, or this misfiles as UNPACKAGED (D-copilot regression)."""
    code = compile("raise ValueError('from a fake frozen stdlib frame')", "<string>", "exec")
    with pytest.raises(ValueError) as exc_info:
      exec(code, {"__name__": "json"})  # noqa: S102 -- deliberate, synthetic file/real stdlib module name

    trail = build_exception_trail(exc_info.value)

    assert trail.entries[0].category is OriginCategory.STDLIB

  def test_the_test_module_itself_is_first_party(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the real pytest process, `get_entrypoint_root()` resolves to pytest's own launcher,
    not this file -- so categorization is pinned to this test module's own root to test the
    FIRST_PARTY branch in isolation from that ambient fact."""
    monkeypatch.setattr(exception_trail_module, "get_entrypoint_root", lambda: get_package_root(__file__))

    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    assert trail.entries[0].category is OriginCategory.FIRST_PARTY

  def test_a_third_party_dependency_in_the_call_path_is_categorized_third_party(self) -> None:
    def _raise_inside_pytest_raises() -> None:
      with pytest.raises(ValueError):
        pass  # pytest.raises' __exit__ raises Failed -- a BaseException, not an Exception, by design

    with pytest.raises(BaseException) as exc_info:
      _raise_inside_pytest_raises()
    trail = build_exception_trail(exc_info.value)

    assert any(entry.category is OriginCategory.THIRD_PARTY for entry in trail.entries)


class TestBuildExceptionTrailChainWalking:
  def test_walk_chain_true_includes_the_cause(self) -> None:
    with pytest.raises(RuntimeError) as exc_info:
      _wrap_and_raise()
    trail = build_exception_trail(exc_info.value, walk_chain=True)

    assert trail.origin.module == __name__
    # cause and context are the same object here (`except ValueError as e: raise ... from e`),
    # the ordinary unambiguous shape -- must not be flagged.
    assert trail.ambiguous_ancestry is False

  def test_walk_chain_false_excludes_the_cause(self) -> None:
    """The cause's frames are STDLIB-categorized (`_wrap_stdlib_error_and_raise`) while the
    wrapping frame's are this test module's own, so the two are distinguishable by category --
    an entry-count comparison alone can't tell "chain walked" from "chain ignored" apart, since
    same-module frames dedupe regardless of which one this test exercises."""
    with pytest.raises(RuntimeError) as exc_info:
      _wrap_stdlib_error_and_raise()
    with_chain = build_exception_trail(exc_info.value, walk_chain=True)
    without_chain = build_exception_trail(exc_info.value, walk_chain=False)

    assert any(entry.category is OriginCategory.STDLIB for entry in with_chain.entries)
    assert not any(entry.category is OriginCategory.STDLIB for entry in without_chain.entries)

  def test_cyclic_context_does_not_infinite_loop(self) -> None:
    """A cyclic implicit `__context__` chain must terminate, not hang."""
    exc_a = ValueError("a")
    exc_b = ValueError("b")
    exc_a.__context__ = exc_b
    exc_b.__context__ = exc_a
    with pytest.raises(ValueError) as exc_info:
      raise exc_a
    trail = build_exception_trail(exc_info.value)  # must return, not hang

    assert trail.entries

  def test_convergent_cause_and_context_is_not_flagged_ambiguous(self) -> None:
    """A different-identity cause/context pair that's still fully ordered ancestry (context's own
    context chain reaches cause) must not be flagged -- only genuinely disjoint ancestry should
    be. Regression test: an earlier version of this check only compared identity, not
    reachability, and wrongly flagged this exact shape."""
    with pytest.raises(RuntimeError) as exc_info:
      _raise_with_convergent_cause_and_context()
    trail = build_exception_trail(exc_info.value, walk_chain=True)

    assert trail.ambiguous_ancestry is False

  def test_context_is_walked_even_when_cause_is_also_set(self) -> None:
    """Both `__cause__` and `__context__` must be walked when they diverge -- following only
    `__cause__` (the pre-fix behavior) would silently drop the `__context__` subtree entirely."""
    with pytest.raises(RuntimeError) as exc_info:
      _raise_with_divergent_cause_and_context()
    trail = build_exception_trail(exc_info.value, walk_chain=True)

    assert trail.ambiguous_ancestry is True
    assert trail.matches(__name__)  # the cause's (this module's) frames were walked
    assert any(e.category is OriginCategory.STDLIB for e in trail.entries)  # so were the context's

  def test_cause_is_ordered_before_context_when_they_diverge(self) -> None:
    """Nothing in the exception objects says which of an unrelated cause/context happened
    first; cause is walked (and so appears) before context, deterministically."""
    with pytest.raises(RuntimeError) as exc_info:
      _raise_with_divergent_cause_and_context()
    trail = build_exception_trail(exc_info.value, walk_chain=True)

    first_party_index = next(i for i, e in enumerate(trail.entries) if e.module == __name__)
    stdlib_index = next(i for i, e in enumerate(trail.entries) if e.category is OriginCategory.STDLIB)
    assert first_party_index < stdlib_index

  def test_nested_context_only_chain_orders_oldest_first(self) -> None:
    """A three-deep pure `__context__` chain (no `__cause__` anywhere) is not ambiguous, and
    orders oldest (outermost) first: this module, then stdlib, then this module again."""
    with pytest.raises(RuntimeError) as exc_info:
      _raise_nested_context_only_chain()
    trail = build_exception_trail(exc_info.value, walk_chain=True)

    assert trail.ambiguous_ancestry is False
    assert trail.entries[0].module == __name__
    assert trail.entries[-1].module == __name__
    stdlib_index = next(i for i, e in enumerate(trail.entries) if e.category is OriginCategory.STDLIB)
    assert 0 < stdlib_index < len(trail.entries) - 1


class TestUnpackagedCategorization:
  def test_exec_with_no_real_file_is_unpackaged(self) -> None:
    code = compile("raise ValueError('from exec')", "<string>", "exec")
    with pytest.raises(ValueError) as exc_info:
      exec(code, {"__name__": "__main__"})  # noqa: S102 -- deliberate, to exercise the UNPACKAGED path
    trail = build_exception_trail(exc_info.value)

    assert trail.entries[0].category is OriginCategory.UNPACKAGED

  def test_exec_with_no_real_file_still_preserves_the_module_name(self) -> None:
    """A frame with a synthetic/missing file must not lose a `__name__` it actually has --
    `_resolve_frame` keeps `module` and `file` as independent unknowns instead of collapsing a
    resolvable module name to `"<unknown>"` just because the file lookup failed."""
    code = compile("raise ValueError('from exec')", "<string>", "exec")
    with pytest.raises(ValueError) as exc_info:
      exec(code, {"__name__": "a_known_module_name"})  # noqa: S102 -- deliberate, synthetic file/known module

    trail = build_exception_trail(exc_info.value)

    assert trail.entries[0].module == "a_known_module_name"


class TestCategorizeInstalledHostApplication:
  def test_installed_host_applications_own_frames_are_first_party(self, tmp_path: Path) -> None:
    """An application launched from within its own `site-packages` install (e.g. `python -m`
    against an installed package) has its own frames' package root land under `site-packages`
    too, matching `entrypoint_root` exactly -- checking the `site-packages` third-party fallback
    before comparing against `entrypoint_root` misfiles the host application's own frames as
    THIRD_PARTY (D-copilot regression)."""
    pkg_dir = tmp_path / "site-packages" / "acme"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    app_file = pkg_dir / "app.py"
    app_file.write_text("")
    entrypoint_root = get_package_root(str(app_file))

    category = exception_trail_module._categorize(  # pyright: ignore[reportPrivateUsage]
      "acme.app", str(app_file), entrypoint_root
    )

    assert category is OriginCategory.FIRST_PARTY

  def test_a_different_installed_dependency_is_still_third_party(self, tmp_path: Path) -> None:
    """A sibling package installed in the same `site-packages` directory as the host application
    (a different root, not matching `entrypoint_root`) must still be THIRD_PARTY."""
    site_packages = tmp_path / "site-packages"
    host_dir = site_packages / "acme"
    host_dir.mkdir(parents=True)
    (host_dir / "__init__.py").write_text("")
    entrypoint_root = get_package_root(str(host_dir / "app.py"))

    dep_dir = site_packages / "requests"
    dep_dir.mkdir(parents=True)
    (dep_dir / "__init__.py").write_text("")
    dep_file = dep_dir / "api.py"
    dep_file.write_text("")

    category = exception_trail_module._categorize(  # pyright: ignore[reportPrivateUsage]
      "requests.api", str(dep_file), entrypoint_root
    )

    assert category is OriginCategory.THIRD_PARTY


class TestCategorizeShadowedStdlibName:
  def test_a_local_module_shadowing_a_stdlib_name_is_not_categorized_stdlib(self, tmp_path: Path) -> None:
    """A first-party module whose top-level name happens to match a stdlib module (e.g. an
    application's own `json.py`) has a real local file, not the interpreter's actual stdlib
    file -- name alone must not be trusted; the file's actual location decides (D-copilot
    regression)."""
    pkg_dir = tmp_path / "acme"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    shadow_file = pkg_dir / "json.py"
    shadow_file.write_text("")
    entrypoint_root = get_package_root(str(shadow_file))

    category = exception_trail_module._categorize(  # pyright: ignore[reportPrivateUsage]
      "json", str(shadow_file), entrypoint_root
    )

    assert category is OriginCategory.FIRST_PARTY

  def test_a_genuine_stdlib_frame_with_a_real_file_is_still_stdlib(self) -> None:
    """The real stdlib `os` module's own file must still categorize STDLIB -- confirms checking
    the file's location doesn't start rejecting genuine stdlib files."""
    # Standard library imports
    import os

    category = exception_trail_module._categorize(  # pyright: ignore[reportPrivateUsage]
      "os", os.__file__, str(Path(os.__file__).parent)
    )

    assert category is OriginCategory.STDLIB


class TestFirstPartyEntry:
  def test_finds_the_first_first_party_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exception_trail_module, "get_entrypoint_root", lambda: get_package_root(__file__))

    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    assert trail.first_party_entry is not None
    assert trail.first_party_entry.category is OriginCategory.FIRST_PARTY

  def test_first_party_entry_is_always_a_member_of_entries_when_present(self) -> None:
    with pytest.raises(json.JSONDecodeError) as exc_info:
      json.loads("{not valid json")
    trail = build_exception_trail(exc_info.value, walk_chain=False)

    if trail.first_party_entry is not None:
      assert trail.first_party_entry in trail.entries

  def test_first_party_still_matches_when_entrypoint_root_is_a_nested_runnable_subpackage(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """`get_entrypoint_root()` deliberately stops at a runnable subpackage's own `__main__.py`
    boundary (e.g. `aeth_ext.central_log_server`), while `get_package_root()` for that
    subpackage's own frames climbs all the way to the top-level package -- simulated here via
    this file's directory (`tests/errors`, nested) standing in for the entrypoint root, versus
    `tests` (this file's real, top-level package root). Comparing these directly (pre-fix) never
    matches FIRST_PARTY for a runnable subpackage's own frames; normalizing entrypoint_root
    through `get_package_root()` first (the fix) does (D-copilot regression)."""
    nested_entrypoint_root = str(Path(__file__).parent)
    monkeypatch.setattr(exception_trail_module, "get_entrypoint_root", lambda: nested_entrypoint_root)

    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    assert trail.entries[0].category is OriginCategory.FIRST_PARTY


class TestMatches:
  def test_matches_returns_all_matching_entries_in_trail_order(self) -> None:
    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    result = trail.matches(__name__)
    assert result == tuple(entry for entry in trail.entries if entry.module == __name__)

  def test_empty_tuple_is_falsy_when_nothing_matches(self) -> None:
    with pytest.raises(ValueError) as exc_info:
      _raise_directly()
    trail = build_exception_trail(exc_info.value)

    result = trail.matches("no.such.module")
    assert result == ()
    assert not result
