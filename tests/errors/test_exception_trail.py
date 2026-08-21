"""Tests for `aeth_ext.errors.exception_trail`."""

# Standard library imports
import json
import re

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


def _raise_with_divergent_cause_and_context() -> None:
  """`__cause__` (this module, via `_raise_directly`) and `__context__` (stdlib, via
  `json.loads`) end up as two unrelated exceptions -- the ambiguous-ancestry shape."""
  try:
    _raise_directly()
  except ValueError as cause_exc:
    try:
      json.loads("{not valid json")
    except json.JSONDecodeError:
      raise RuntimeError("ambiguous") from cause_exc


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
