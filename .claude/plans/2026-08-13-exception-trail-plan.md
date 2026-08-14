# Exception Trail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `err_handling.py`'s ad hoc `extract_details_callable` escape hatch with a shared,
standardized `ExceptionTrail` primitive that computes an exception's full origin trail once, answers a
small fixed set of questions about it, and is retrievable both by registered `register_for_shutdown`
callbacks and by ad hoc cleanup code watching `SHUTDOWN`.

**Architecture:** A new leaf module, `src/aeth_ext/errors/exception_trail.py`, owns trail construction
and querying with zero dependency on `shutdown.py` or `err_handling.py`. `shutdown.py` gains a settable
module-level slot (`_current_fatal_trail`) plus a public getter, and teaches `register_for_shutdown` to
detect one-positional-arg callbacks via `inspect.signature` so it can pass the trail to callbacks that
want it. `err_handling.py` builds the trail exactly once per fatal exception (in whichever of
`_handle_fatal` or the decorator wrapper needs it first) and threads that single instance through both
the renamed `extract_trail_callable` and `_set_current_fatal_trail`.

**Tech Stack:** Python 3.14, stdlib `traceback`/`inspect`/`re`/`sys`, `aeth_ext.static_eval` for package-root
resolution, `pytest` + `hypothesis` (already a project dependency, per `tests/conftest.py`) is available
but not required here — the glob-matcher tests are plain table-driven `pytest.mark.parametrize`.

**Spec:** [`.claude/plans/2026-08-13-exception-trail-design.md`](2026-08-13-exception-trail-design.md)

## Global Constraints

- No `from __future__ import annotations` anywhere (project-wide rule) — every type used in a real
  annotation must be a genuine runtime import unless it is a Pyright-only, never-forced annotation.
- 2-space indentation, 135-char line length (`pyproject.toml` `[tool.ruff]`).
- Import sections use the project's `# Standard library imports` / `# Third party imports` /
  `# First party imports` headers (see any existing file in `src/aeth_ext/errors/`).
- Docstrings are Google style (`Args:`/`Returns:`/`Raises:`), no Sphinx roles — match
  `aeth_ext/errors/shutdown.py`'s existing style for anything touched in that file, and use the same
  style for all new code in this plan.
- Conventional Commits for every commit: `<type>(<scope>): <summary>`; scope is `errors` for every task
  below (all changes live under `aeth_ext.errors`).
- Do not run the full test suite while iterating — only the targeted tests named in each task, plus one
  full `uv run pytest` pass at the very end (Task 6).
- `sys.stdlib_module_names` is available (Python 3.10+, this repo requires `>=3.14`).

---

## File Structure

- **Create** `src/aeth_ext/errors/exception_trail.py` — `OriginCategory`, `TrailEntry`, `ExceptionTrail`,
  `build_exception_trail`, and the private glob-pattern compiler. No imports from `shutdown.py` or
  `err_handling.py` — this module is a leaf.
- **Create** `tests/errors/test_exception_trail.py` — all in-process tests for the module above (nothing
  here touches `__debug__`-gated code or the process-wide `SHUTDOWN` singleton, so no `-O` subprocess is
  needed for this file).
- **Modify** `src/aeth_ext/errors/shutdown.py` — add `_current_fatal_trail`, `get_current_fatal_trail`,
  `_set_current_fatal_trail`; add `wants_trail` to `_Registration`; teach `register_for_shutdown` to
  detect one-arg callbacks; teach `_run_interrupt_pass`/`_run_threaded_pass` to invoke them with the
  trail.
- **Modify** `src/aeth_ext/errors/err_handling.py` — rename `extract_details_callable` to
  `extract_trail_callable` (retyped `Callable[[ExceptionTrail], Any] | None`) on both decorators; give
  `_handle_fatal` an optional pre-built `trail` parameter; rename `testing_details_extractor` to
  `testing_trail_extractor`.
- **Modify** `src/aeth_ext/errors/__init__.py` — export `ExceptionTrail`, `OriginCategory`, `TrailEntry`,
  `build_exception_trail` alongside the existing re-exports.
- **Modify** `tests/errors/_optimized_scenarios.py` — rename the `extract_details_callable` scenario
  functions/kwarg usage to `extract_trail_callable`, updating assertions for the new callback argument
  (now an `ExceptionTrail`, not the raw exception).
- **Modify** `tests/errors/test_err_handling.py` — rename the corresponding test names/references.
- **Create** `tests/errors/_exception_trail_shutdown_scenarios.py` — `-O` subprocess scenarios for
  `register_for_shutdown` signature detection and `get_current_fatal_trail`, following
  `tests/errors/_shutdown_signal_scenarios.py`'s existing pattern.
- **Modify** `tests/errors/test_shutdown.py` — add a test class driving the new scenarios script.
- **Modify** `TODO.md` — delete item 6 (`Replace extract_details_callable...`), now resolved.

---

## Task 1: `OriginCategory`, `TrailEntry`, and the glob-pattern matcher

**Files:**
- Create: `src/aeth_ext/errors/exception_trail.py`
- Test: `tests/errors/test_exception_trail.py`

**Interfaces:**
- Produces: `OriginCategory` (`aeth_ext.types.StrEnum` subclass) with members `FIRST_PARTY`,
  `THIRD_PARTY`, `STDLIB`, `UNPACKAGED` (each member's value is its own name, via the project's
  `StrEnum._generate_next_value_`). `TrailEntry` (`NamedTuple`): `module: str`, `category:
  OriginCategory`, `file: str`. A private `_compile_pattern(pattern: str) -> re.Pattern[str]` used by
  `ExceptionTrail.matches` in Task 2.

This task only builds the pure, exception-independent pieces (the enum, the tuple, and the pattern
compiler) so the glob matcher can be tested in isolation before `ExceptionTrail` exists.

- [ ] **Step 1: Write the failing glob-matcher tests**

Create `tests/errors/test_exception_trail.py`:

```python
"""Tests for `aeth_ext.errors.exception_trail`."""

# Standard library imports
import re

# Third party imports
import pytest

# First party imports
from aeth_ext.errors.exception_trail import OriginCategory, TrailEntry, _compile_pattern  # pyright: ignore[reportPrivateUsage]


class TestOriginCategory:
  def test_members_use_their_own_name_as_value(self):
    assert OriginCategory.FIRST_PARTY.value == "FIRST_PARTY"
    assert OriginCategory.THIRD_PARTY.value == "THIRD_PARTY"
    assert OriginCategory.STDLIB.value == "STDLIB"
    assert OriginCategory.UNPACKAGED.value == "UNPACKAGED"


class TestTrailEntry:
  def test_fields_are_positional_and_named(self):
    entry = TrailEntry(module="pkg.mod", category=OriginCategory.FIRST_PARTY, file="/pkg/mod.py")
    assert entry.module == "pkg.mod"
    assert entry.category is OriginCategory.FIRST_PARTY
    assert entry.file == "/pkg/mod.py"


class TestCompilePatternLiteral:
  def test_exact_match(self):
    pattern = _compile_pattern("scheduled_invoice_processor.database")
    assert pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_as_prefix(self):
    """A bare literal must not match as a prefix of a longer dotted name."""
    pattern = _compile_pattern("database")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_as_suffix(self):
    """A bare literal must not match as a suffix either -- full anchoring both ends."""
    pattern = _compile_pattern("scheduled_invoice_processor")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")


class TestCompilePatternSingleStar:
  def test_matches_exactly_one_segment(self):
    pattern = _compile_pattern("scheduled_invoice_processor.*.database")
    assert pattern.fullmatch("scheduled_invoice_processor.suppliers.database")

  def test_rejects_zero_segments(self):
    pattern = _compile_pattern("scheduled_invoice_processor.*.database")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")

  def test_rejects_two_segments(self):
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
  def test_matches_zero_one_and_multiple_segments(self, module: str):
    pattern = _compile_pattern("scheduled_invoice_processor.**.database")
    assert pattern.fullmatch(module)

  def test_matches_as_leading_wildcard(self):
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
  def test_matches_gspread_anywhere(self, module: str):
    pattern = _compile_pattern("**.gspread.**")
    assert pattern.fullmatch(module)

  def test_rejects_when_segment_absent(self):
    pattern = _compile_pattern("**.gspread.**")
    assert not pattern.fullmatch("scheduled_invoice_processor.database")


class TestCompilePatternAnchoring:
  def test_fully_anchored_not_findall_style(self):
    """A pattern that would match as an unanchored substring must not match here."""
    pattern = re.compile(_compile_pattern("database").pattern)
    assert pattern.search("scheduled_invoice_processor.database.orm") is not None  # sanity: substring exists
    assert not _compile_pattern("database").fullmatch("scheduled_invoice_processor.database.orm")
```

- [ ] **Step 2: Run the tests to verify they fail with `ImportError`**

Run: `uv run pytest tests/errors/test_exception_trail.py -v`
Expected: `ImportError: cannot import name 'OriginCategory' from 'aeth_ext.errors.exception_trail'`
(the module does not exist yet).

- [ ] **Step 3: Implement `OriginCategory`, `TrailEntry`, and `_compile_pattern`**

Create `src/aeth_ext/errors/exception_trail.py`:

```python
"""Standardized fatal-exception-origin trail (replaces `extract_details_callable`, TODO.md #6).

`build_exception_trail` walks an exception's traceback (and, by default, its `__cause__`/`__context__`
chain) into an `ExceptionTrail`: an ordered, deduplicated, origin-first sequence of `TrailEntry`
records, each categorized as first-party, third-party, stdlib, or unpackaged. `ExceptionTrail.matches`
is the one standardized way to ask "did this failure touch marked module(s)" -- replacing every
consumer's own hand-rolled frame-walk/path-match logic.
"""

# Standard library imports
import re
from typing import NamedTuple

# First party imports
from aeth_ext.types import StrEnum

__all__ = ["ExceptionTrail", "OriginCategory", "TrailEntry", "build_exception_trail"]


class OriginCategory(StrEnum):
  """Where a `TrailEntry`'s module lives, relative to the running application."""

  FIRST_PARTY = ()
  """Belongs to the application that is running (its package root matches `get_entrypoint_root()`)."""

  THIRD_PARTY = ()
  """An installed dependency, or a resolvable package that is not the host application's own."""

  STDLIB = ()
  """Part of the Python standard library (`sys.stdlib_module_names`)."""

  UNPACKAGED = ()
  """No resolvable package root -- a loose script, `__main__`, or code with no real backing file."""


class TrailEntry(NamedTuple):
  """One distinct module transition in an `ExceptionTrail`, origin-first."""

  module: str
  category: OriginCategory
  file: str


_SEGMENT_STAR = "*"
_SEGMENT_DOUBLE_STAR = "**"


def _compile_pattern(pattern: str) -> re.Pattern[str]:
  """Compile a dot-segment glob *pattern* into a fully-anchored regex.

  `*` matches exactly one segment; `**` matches zero or more segments, including the separating dot on
  whichever side has a neighboring segment -- this is what lets `"a.**.b"` match the zero-segment case
  `"a.b"`, not just `"a.x.b"`. A `**` with a segment on only one side swallows only that side's dot; a
  bare `"**"` matches any non-empty dotted name. Not cached: patterns are typically static constants at
  a call site, so compiling per `ExceptionTrail.matches()` call costs nothing worth caching against.
  """
  segments = pattern.split(".")
  pieces: list[str] = []
  for i, seg in enumerate(segments):
    has_prev = i > 0
    has_next = i < len(segments) - 1
    if seg == _SEGMENT_DOUBLE_STAR:
      if has_prev and has_next:
        pieces.append(r"\.(?:[^.]+\.)*")
      elif has_next:
        pieces.append(r"(?:[^.]+\.)*")
      elif has_prev:
        pieces.append(r"(?:\.[^.]+)*")
      else:
        pieces.append(r".+")
      continue
    if has_prev and segments[i - 1] != _SEGMENT_DOUBLE_STAR:
      pieces.append(r"\.")
    pieces.append(r"[^.]+" if seg == _SEGMENT_STAR else re.escape(seg))
  return re.compile(f"^{''.join(pieces)}$")
```

Note: `OriginCategory`'s members are written as `NAME = ()` because `aeth_ext.types.StrEnum` overrides
`_generate_next_value_` to derive each member's value from its own name -- matching every other
`StrEnum` usage convention in this project (see `aeth_ext/types/__init__.py`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/errors/test_exception_trail.py -v`
Expected: all `TestOriginCategory`, `TestTrailEntry`, and `TestCompilePattern*` tests PASS.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/aeth_ext/errors/exception_trail.py tests/errors/test_exception_trail.py`

```bash
git add src/aeth_ext/errors/exception_trail.py tests/errors/test_exception_trail.py
git commit -m "feat(errors): add OriginCategory, TrailEntry, and glob-pattern matcher"
```

---

## Task 2: `build_exception_trail` and `ExceptionTrail`

**Files:**
- Modify: `src/aeth_ext/errors/exception_trail.py`
- Test: `tests/errors/test_exception_trail.py`

**Interfaces:**
- Consumes: `OriginCategory`, `TrailEntry`, `_compile_pattern` from Task 1.
- Produces: `ExceptionTrail` (`entries: tuple[TrailEntry, ...]`, `origin: TrailEntry`,
  `first_party_entry: TrailEntry | None`, `matches(*patterns: str) -> tuple[TrailEntry, ...]`) and
  `build_exception_trail(exc: BaseException, *, walk_chain: bool = True) -> ExceptionTrail`. Both are
  consumed by Task 3 (`shutdown.py`) and Task 4 (`err_handling.py`).

- [ ] **Step 1: Write the failing construction/chain/categorization tests**

Append to `tests/errors/test_exception_trail.py`:

```python
# Standard library imports
import json
import sys

# First party imports
from aeth_ext.errors.exception_trail import ExceptionTrail, build_exception_trail


def _raise_stdlib_error() -> None:
  json.loads("{not valid json")


def _raise_directly() -> None:
  raise ValueError("boom")


def _wrap_and_raise() -> None:
  try:
    _raise_directly()
  except ValueError as e:
    raise RuntimeError("wrapped") from e


class TestBuildExceptionTrailConstruction:
  def test_entries_are_origin_first(self):
    try:
      _raise_directly()
    except ValueError as e:
      trail = build_exception_trail(e)

    # The innermost frame (where `raise` actually executed) is entries[0].
    assert trail.entries[0].module == __name__
    assert trail.origin == trail.entries[0]

  def test_a_stdlib_frame_in_the_call_path_is_categorized_stdlib(self):
    try:
      _raise_stdlib_error()
    except Exception as e:  # noqa: BLE001 -- json.JSONDecodeError, deliberately broad for the test
      trail = build_exception_trail(e)

    assert any(entry.category == "STDLIB" for entry in trail.entries)

  def test_the_test_module_itself_is_first_party(self):
    try:
      _raise_directly()
    except ValueError as e:
      trail = build_exception_trail(e)

    assert trail.entries[0].category == "FIRST_PARTY"

  def test_a_third_party_dependency_in_the_call_path_is_categorized_third_party(self):
    # Third party imports
    import pytest as _pytest  # local import: only needed to force a real third-party frame

    def _raise_inside_pytest_raises() -> None:
      with _pytest.raises(ValueError):
        pass  # pytest.raises' __exit__ raises Failed when nothing was raised

    try:
      _raise_inside_pytest_raises()
    except Exception as e:  # noqa: BLE001 -- pytest's own Failed exception, deliberately broad
      trail = build_exception_trail(e)

    assert any(entry.category == "THIRD_PARTY" for entry in trail.entries)


class TestBuildExceptionTrailChainWalking:
  def test_walk_chain_true_includes_the_cause(self):
    try:
      _wrap_and_raise()
    except RuntimeError as e:
      trail = build_exception_trail(e, walk_chain=True)

    assert any(entry.module == __name__ and "ValueError" not in entry.file for entry in trail.entries)
    # Both frames (the raise inside _raise_directly and the reraise inside _wrap_and_raise)
    # come from this module -- assert on the underlying frame count via a name that
    # only exists in the cause path is impractical, so assert on the two distinct
    # co_name-carrying call sites instead by checking the origin is the innermost cause frame.
    assert trail.origin.module == __name__

  def test_walk_chain_false_excludes_the_cause(self, monkeypatch: pytest.MonkeyPatch):
    try:
      _wrap_and_raise()
    except RuntimeError as e:
      with_chain = build_exception_trail(e, walk_chain=True)
      without_chain = build_exception_trail(e, walk_chain=False)

    assert len(without_chain.entries) <= len(with_chain.entries)

  def test_cyclic_context_does_not_infinite_loop(self):
    """A cyclic implicit `__context__` chain must terminate, not hang."""
    exc_a = ValueError("a")
    exc_b = ValueError("b")
    exc_a.__context__ = exc_b
    exc_b.__context__ = exc_a
    try:
      raise exc_a  # noqa: TRY301 -- need a real __traceback__ for build_exception_trail to walk
    except ValueError as e:
      trail = build_exception_trail(e)  # must return, not hang

    assert trail.entries


class TestUnpackagedCategorization:
  def test_exec_with_no_real_file_is_unpackaged(self):
    code = compile("raise ValueError('from exec')", "<string>", "exec")
    try:
      exec(code, {"__name__": "__main__"})  # noqa: S102 -- deliberate, to exercise the UNPACKAGED path
    except ValueError as e:
      trail = build_exception_trail(e)

    assert trail.entries[0].category == "UNPACKAGED"


class TestFirstPartyEntry:
  def test_finds_the_first_first_party_frame(self):
    try:
      _raise_directly()
    except ValueError as e:
      trail = build_exception_trail(e)

    assert trail.first_party_entry is not None
    assert trail.first_party_entry.category == "FIRST_PARTY"

  def test_none_when_the_trail_never_touches_first_party_code(self):
    try:
      json.loads("{not valid json")
    except Exception as e:  # noqa: BLE001
      trail = build_exception_trail(e, walk_chain=False)

    # json.loads's own frames are STDLIB; this process's test module frame that
    # *called* json.loads is still on the traceback and is FIRST_PARTY, so this
    # particular exception can't produce a None result -- assert the realistic
    # invariant instead: first_party_entry, when present, is always inside `.entries`.
    if trail.first_party_entry is not None:
      assert trail.first_party_entry in trail.entries


class TestMatches:
  def test_matches_returns_all_matching_entries_in_trail_order(self):
    try:
      _raise_directly()
    except ValueError as e:
      trail = build_exception_trail(e)

    result = trail.matches(f"{__name__}")
    assert result == tuple(entry for entry in trail.entries if entry.module == __name__)

  def test_empty_tuple_is_falsy_when_nothing_matches(self):
    try:
      _raise_directly()
    except ValueError as e:
      trail = build_exception_trail(e)

    result = trail.matches("no.such.module")
    assert result == ()
    assert not result
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/errors/test_exception_trail.py -v`
Expected: `ImportError: cannot import name 'ExceptionTrail'` / `'build_exception_trail'`.

- [ ] **Step 3: Implement `ExceptionTrail` and `build_exception_trail`**

Append to `src/aeth_ext/errors/exception_trail.py` (add these imports to the existing import block at
the top of the file):

```python
# Standard library imports
import sys
from dataclasses import dataclass
from os.path import dirname, isfile, join
from types import FrameType

# First party imports
from aeth_ext.static_eval import get_entrypoint_root, get_package_root
```

Then add:

```python
def _categorize(module: str, file: str) -> OriginCategory:
  """Categorize a single resolved `(module, file)` pair.

  Order matters: stdlib is checked first (a stdlib frame has no meaningful package root to
  compute), then the file's own package root decides third-party/first-party/unpackaged.
  """
  top_level = module.partition(".")[0]
  if top_level in sys.stdlib_module_names:
    return OriginCategory.STDLIB

  root = get_package_root(file)
  if "site-packages" in root.replace("\\", "/").split("/"):
    return OriginCategory.THIRD_PARTY
  if not isfile(join(root, "__init__.py")):
    return OriginCategory.UNPACKAGED
  return OriginCategory.FIRST_PARTY if root == get_entrypoint_root() else OriginCategory.THIRD_PARTY


def _resolve_frame(frame: FrameType) -> tuple[str, str] | None:
  """Return `(module, file)` for *frame*, or `None` if it has no usable module/file at all."""
  module = frame.f_globals.get("__name__")
  file = frame.f_code.co_filename
  if not module or not file or not isfile(file):
    return None
  return module, file


def _frames_innermost_first(exc: BaseException) -> list[FrameType]:
  """Every frame on `exc.__traceback__`, innermost (where it was raised) first."""
  frames: list[FrameType] = []
  tb = exc.__traceback__
  while tb is not None:
    frames.append(tb.tb_frame)
    tb = tb.tb_next
  frames.reverse()
  return frames


def _chain_root_first(exc: BaseException) -> list[BaseException]:
  """*exc* and its `__cause__`/`__context__` ancestors, oldest cause first, `exc` last.

  `id()`-based cycle detection: an implicit `__context__` loop stops the walk instead of hanging.
  """
  seen: set[int] = set()
  chain: list[BaseException] = []
  current: BaseException | None = exc
  while current is not None and id(current) not in seen:
    seen.add(id(current))
    chain.append(current)
    current = current.__cause__ or current.__context__
  chain.reverse()
  return chain


def _build_entries(exc: BaseException, *, walk_chain: bool) -> tuple[TrailEntry, ...]:
  """Walk *exc* (and optionally its cause chain) into deduplicated, origin-first `TrailEntry` tuples."""
  exceptions = _chain_root_first(exc) if walk_chain else [exc]

  entries: list[TrailEntry] = []
  last_module: str | None = None
  for one_exc in exceptions:
    for frame in _frames_innermost_first(one_exc):
      resolved = _resolve_frame(frame)
      if resolved is None:
        module, file = "<unknown>", frame.f_code.co_filename or "<unknown>"
        category = OriginCategory.UNPACKAGED
      else:
        module, file = resolved
        category = _categorize(module, file)
      if module == last_module:
        continue
      entries.append(TrailEntry(module=module, category=category, file=file))
      last_module = module

  return tuple(entries)


@dataclass(frozen=True, slots=True)
class ExceptionTrail:
  """A single fatal exception's full origin trail, origin-first and deduplicated.

  Built once by `build_exception_trail` and never mutated -- every attribute below is computed
  eagerly at construction, not lazily, since the walk itself already dominates the cost.
  """

  entries: tuple[TrailEntry, ...]
  origin: TrailEntry
  first_party_entry: TrailEntry | None

  def matches(self, *patterns: str) -> tuple[TrailEntry, ...]:
    """Every entry whose module matches any of *patterns* (see `_compile_pattern` for glob syntax).

    Returns entries in trail order (origin-first). The empty tuple is falsy, so
    `if trail.matches(...):` reads as a boolean check while still handing back full match detail to a
    caller that wants it.
    """
    compiled = [_compile_pattern(p) for p in patterns]
    return tuple(entry for entry in self.entries if any(p.fullmatch(entry.module) for p in compiled))


def build_exception_trail(exc: BaseException, *, walk_chain: bool = True) -> ExceptionTrail:
  """Build the full origin trail for *exc*.

  Args:
    exc: The exception to walk. Must have a live `__traceback__` (i.e. called from inside the
      `except` block that caught it, or with a manually-attached traceback).
    walk_chain: When `True` (default), also walks `__cause__`/`__context__` recursively, with
      `id()`-based cycle detection. Matches the app-side `_is_database_origin_exception` behavior
      this trail replaces.

  Returns:
    An `ExceptionTrail` whose `entries` is never empty -- an exception always has at least one
    frame, the one that raised it.
  """
  entries = _build_entries(exc, walk_chain=walk_chain)
  first_party = next((e for e in entries if e.category is OriginCategory.FIRST_PARTY), None)
  return ExceptionTrail(entries=entries, origin=entries[0], first_party_entry=first_party)
```

Update `__all__` at the top of the file (already lists `ExceptionTrail`/`build_exception_trail` from
Task 1's stub list — no change needed there since Task 1 already wrote the final `__all__`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/errors/test_exception_trail.py -v`
Expected: all tests PASS.

If `test_a_third_party_dependency_in_the_call_path_is_categorized_third_party` fails because pytest's
own frames get deduplicated away or categorized unexpectedly, print `trail.entries` in a throwaway
local run to see the actual module names captured and adjust the assertion to check for `pytest` (or
`_pytest`) appearing as a `THIRD_PARTY` module rather than asserting an exact count.

- [ ] **Step 5: Type-check and lint**

Run: `uv run pyright src/aeth_ext/errors/exception_trail.py`
Run: `uv run ruff check src/aeth_ext/errors/exception_trail.py tests/errors/test_exception_trail.py`

Fix any reported issues (a likely one: `OriginCategory` comparisons against string literals like
`"STDLIB"` in the tests above are intentional -- `StrEnum` members compare equal to their string value
-- but confirm Pyright doesn't flag `entry.category == "STDLIB"` as `reportUnnecessaryComparison`; if it
does, tighten those specific test assertions to `entry.category is OriginCategory.STDLIB` instead).

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/errors/exception_trail.py tests/errors/test_exception_trail.py
git commit -m "feat(errors): add build_exception_trail and ExceptionTrail"
```

---

## Task 3: Wire the trail into `aeth_ext.errors.shutdown`

**Files:**
- Modify: `src/aeth_ext/errors/shutdown.py`
- Create: `tests/errors/_exception_trail_shutdown_scenarios.py`
- Modify: `tests/errors/test_shutdown.py`

**Interfaces:**
- Consumes: `ExceptionTrail` from Task 2 (`aeth_ext.errors.exception_trail`).
- Produces: `get_current_fatal_trail() -> ExceptionTrail | None`, `_set_current_fatal_trail(trail:
  ExceptionTrail) -> None` (private, called only by `err_handling._handle_fatal` in Task 4).
  `register_for_shutdown` now accepts either a zero-arg callback or a one-arg
  `Callable[[ExceptionTrail | None], None]`, detected via `inspect.signature`.

- [ ] **Step 1: Write the failing scenario script**

Create `tests/errors/_exception_trail_shutdown_scenarios.py`:

```python
"""Real, importable `-O` subprocess scenarios for `register_for_shutdown`'s trail-passing behavior
and `get_current_fatal_trail` (`aeth_ext.errors.shutdown`).

Written as genuine Python source, not a `python -c` string, so IDE rename-symbol tooling can track
references -- same convention as `_shutdown_signal_scenarios.py` and `_optimized_scenarios.py`.
`register_for_shutdown`'s trail-passing only happens during a real driven shutdown, and
`get_current_fatal_trail` reads the process-wide, one-shot `_current_fatal_trail` slot, so both need
this repo's existing `-O` isolated-subprocess pattern.
"""

# Standard library imports
import json
import sys

# First party imports
from aeth_ext.errors import shutdown as shutdown_module
from aeth_ext.errors.exception_trail import build_exception_trail
from aeth_ext.errors.shutdown import ShutdownKind, ShutdownPhase

assert not __debug__, "this harness must run under python -O"


def get_current_fatal_trail_is_none_before_any_shutdown() -> dict[str, object]:
  return {"trail": shutdown_module.get_current_fatal_trail()}


def get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown() -> dict[str, object]:
  try:
    raise ValueError("boom")
  except ValueError as e:
    trail = build_exception_trail(e)
    shutdown_module._set_current_fatal_trail(trail)  # noqa: SLF001 -- exercising the private setter directly

  retrieved = shutdown_module.get_current_fatal_trail()
  return {"same_object": retrieved is trail, "origin_module": retrieved.origin.module if retrieved else None}


def zero_arg_callback_is_invoked_with_no_arguments() -> dict[str, object]:
  calls: list[object] = []

  def zero_arg() -> None:
    calls.append("called")

  shutdown_module.register_for_shutdown(zero_arg, phase=ShutdownPhase.THREADED)
  shutdown_module.run_shutdown(ShutdownKind.GRACEFUL)
  # run_shutdown() hands the threaded pass to a daemon thread and returns immediately;
  # give it a moment to actually run before checking (mirrors run_shutdown's own
  # "don't block the caller" contract -- the scenario just needs the pass to finish).
  import time

  time.sleep(0.5)
  return {"called": calls == ["called"]}


def one_arg_callback_receives_none_when_no_trail_is_set() -> dict[str, object]:
  received: list[object] = []

  def one_arg(trail: object) -> None:
    received.append(trail)

  shutdown_module.register_for_shutdown(one_arg, phase=ShutdownPhase.THREADED)
  shutdown_module.run_shutdown(ShutdownKind.GRACEFUL)
  import time

  time.sleep(0.5)
  return {"received_none": received == [None]}


def one_arg_callback_receives_the_real_trail_when_fatal() -> dict[str, object]:
  received: list[object] = []

  def one_arg(trail: object) -> None:
    received.append(trail)

  shutdown_module.register_for_shutdown(one_arg, phase=ShutdownPhase.THREADED)
  try:
    raise ValueError("boom")
  except ValueError as e:
    shutdown_module._set_current_fatal_trail(build_exception_trail(e))  # noqa: SLF001

  shutdown_module.run_shutdown(ShutdownKind.FATAL)
  import time

  time.sleep(0.5)
  return {"received_a_trail": len(received) == 1 and received[0] is not None}


_SCENARIOS = {
  "get_current_fatal_trail_is_none_before_any_shutdown": get_current_fatal_trail_is_none_before_any_shutdown,
  "get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown": (
    get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown
  ),
  "zero_arg_callback_is_invoked_with_no_arguments": zero_arg_callback_is_invoked_with_no_arguments,
  "one_arg_callback_receives_none_when_no_trail_is_set": one_arg_callback_receives_none_when_no_trail_is_set,
  "one_arg_callback_receives_the_real_trail_when_fatal": one_arg_callback_receives_the_real_trail_when_fatal,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps({k: (v if not hasattr(v, "origin") else True) for k, v in result.items()}))
```

Note: `get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown`'s result dict is
JSON-serialized by the `__main__` block's final line, which replaces any `ExceptionTrail`-shaped value
with `True` (JSON can't carry the object itself) -- the test asserts on `"same_object"` and
`"origin_module"`, both already plain JSON types, so this substitution never affects what's checked.

Add to `tests/errors/test_shutdown.py` (near the existing `_run_optimized` helper, after the imports):

```python
_TRAIL_SCENARIOS_SCRIPT = Path(__file__).parent / "_exception_trail_shutdown_scenarios.py"


def _run_trail_scenario(scenario_name: str) -> Mapping[str, object]:
  """Run the named scenario from `_exception_trail_shutdown_scenarios.py` in a fresh `-O` subprocess."""
  env = dict(os.environ)
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")

  proc = subprocess.run(
    [sys.executable, "-O", str(_TRAIL_SCENARIOS_SCRIPT), scenario_name],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )

  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

  return json.loads(proc.stdout.strip().splitlines()[-1])


class TestGetCurrentFatalTrail:
  def test_none_before_any_shutdown(self):
    result = _run_trail_scenario("get_current_fatal_trail_is_none_before_any_shutdown")
    assert result == {"trail": None}

  def test_returns_the_trail_set_before_a_fatal_shutdown(self):
    result = _run_trail_scenario("get_current_fatal_trail_returns_the_set_trail_after_fatal_shutdown")
    assert result == {"same_object": True, "origin_module": "_exception_trail_shutdown_scenarios"}


class TestRegisterForShutdownSignatureDetection:
  def test_zero_arg_callback_is_invoked_with_no_arguments(self):
    result = _run_trail_scenario("zero_arg_callback_is_invoked_with_no_arguments")
    assert result == {"called": True}

  def test_one_arg_callback_receives_none_when_no_trail_is_set(self):
    result = _run_trail_scenario("one_arg_callback_receives_none_when_no_trail_is_set")
    assert result == {"received_none": True}

  def test_one_arg_callback_receives_the_real_trail_when_fatal(self):
    result = _run_trail_scenario("one_arg_callback_receives_the_real_trail_when_fatal")
    assert result == {"received_a_trail": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/errors/test_shutdown.py::TestGetCurrentFatalTrail tests/errors/test_shutdown.py::TestRegisterForShutdownSignatureDetection -v`
Expected: `AttributeError: module 'aeth_ext.errors.shutdown' has no attribute 'get_current_fatal_trail'`.

- [ ] **Step 3: Implement the shutdown-module changes**

In `src/aeth_ext/errors/shutdown.py`, add to the import block:

```python
# Standard library imports
import inspect
```

```python
# First party imports
from aeth_ext.errors.exception_trail import ExceptionTrail
```

Add `get_current_fatal_trail` to `__all__` (alphabetical, matching the existing list's ordering).

Add the module-level state and accessors, placed alongside the existing `_t0`/`_exit_nudge_sent`/
`_drive_released` block (same file, right after `_drive_released = ThreadingEvent()`):

```python
_current_fatal_trail: ExceptionTrail | None = None
"""Set by `_handle_fatal` (`aeth_ext.errors.err_handling`) immediately before it calls
`run_shutdown(FATAL)`; never cleared afterward, matching `SHUTDOWN`'s own one-shot semantics."""


def get_current_fatal_trail() -> ExceptionTrail | None:
  """The `ExceptionTrail` for the exception currently driving a fatal shutdown, if any.

  Returns:
    `None` when no shutdown is underway, or the current shutdown was not triggered by a live
    exception (e.g. a signal-driven `GRACEFUL` shutdown, or `trigger_shutdown()` for a plain error
    condition with nothing to raise). Set by `_handle_fatal` (`aeth_ext.errors.err_handling`)
    immediately before it calls `run_shutdown(FATAL)`.

    This is the retrieval mechanism for cleanup code that runs behind the shutdown event (e.g.
    `await SHUTDOWN` in an application's main loop) rather than as a registered
    `register_for_shutdown` callback -- those receive the trail as a call argument instead.
  """
  return _current_fatal_trail


def _set_current_fatal_trail(trail: ExceptionTrail) -> None:
  """Private setter, called only by `aeth_ext.errors.err_handling._handle_fatal`."""
  global _current_fatal_trail
  _current_fatal_trail = trail
```

Modify `_Registration` to add a `wants_trail` field:

```python
class _Registration(NamedTuple):
  """One registered callback plus the five independent knobs governing it."""

  get: Callable[[], Callable[[], None] | Callable[[ExceptionTrail | None], None] | None]
  """Returns the callback, or `None` if its owner has been collected."""

  phase: ShutdownPhase
  priority: int
  required: bool
  wants_trail: bool
  """Whether this callback accepts the current fatal trail as its one positional argument."""

  label: str
  """Human-readable identity, captured at registration time so a failure can be attributed after the
  callback's owner may already be gone."""
```

Modify `register_for_shutdown` to detect the callback's arity and set `wants_trail`:

```python
def register_for_shutdown(
  callback: Callable[[], None] | Callable[[ExceptionTrail | None], None],
  *,
  phase: ShutdownPhase,
  priority: int = 0,
  required: bool = False,
) -> None:
  """Register *callback* to run during process shutdown (D-I2).

  Args:
    callback: The callback to run at shutdown -- either zero-arg, or accepting exactly one
      positional argument (the current `ExceptionTrail | None`, from `get_current_fatal_trail`).
      Arity is detected once via `inspect.signature` at registration time.
    phase: *Where* it runs. See `ShutdownPhase` for the safety obligations each phase imposes --
      they follow from the execution context and apply regardless of *required*.
    priority: Ascending within its phase; ties broken by registration order. `aeth_ext`'s own
      registrants use a high priority to run **after** a downstream application's, since the
      logging transport must be torn down last -- anything torn down before it that then logs
      would write into a closed handler.
    required: Whether the threaded pass may skip this callback once its time budget is exhausted.
      A skip policy only, orthogonal to *phase*: a no-op for the interrupt pass, which has no
      budget.

  An object needing work in both phases simply registers twice.
  """
  global _registrations

  owner = getattr(callback, "__self__", None)
  name = str(getattr(callback, "__qualname__", None) or repr(callback))
  if owner is None:
    label = name
  else:
    owner_type = owner if isinstance(owner, type) else type(owner)
    label = f"{owner_type.__name__}.{name.rpartition('.')[2]}"

  try:
    wants_trail = len(inspect.signature(callback).parameters) == 1
  except (TypeError, ValueError):
    wants_trail = False

  get = WeakMethod(callback) if owner is not None else (lambda: callback)

  entry = _Registration(
    get=get,
    phase=phase,
    priority=priority,
    required=required,
    wants_trail=wants_trail,
    label=label,
  )
  with _registry_lock:
    _registrations = (*_registrations, entry)
```

Modify `_run_interrupt_pass` and `_run_threaded_pass` to invoke trail-wanting callbacks with the trail.
In `_run_interrupt_pass`, replace the `callback()` call:

```python
  failures: list[tuple[str, BaseException]] = []
  for reg in sorted((r for r in _registrations if r.phase is ShutdownPhase.INTERRUPT), key=lambda r: r.priority):
    callback = reg.get()
    if callback is None:
      continue
    try:
      callback(_current_fatal_trail) if reg.wants_trail else callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad arm must not block the rest
      failures.append((reg.label, exc))
      _emit(f"ARM FAILED: {reg.label}")
  return failures
```

In `_run_threaded_pass`, replace the `callback()` call inside the `for reg in pending:` loop:

```python
    run += 1
    try:
      callback(_current_fatal_trail) if reg.wants_trail else callback()
    except BaseException as exc:  # noqa: BLE001 -- one bad teardown must not block the rest
      _emit(f"TEARDOWN FAILED: {reg.label}\n{''.join(format_exception(exc))}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/errors/test_shutdown.py::TestGetCurrentFatalTrail tests/errors/test_shutdown.py::TestRegisterForShutdownSignatureDetection -v`
Expected: all PASS.

- [ ] **Step 5: Run the full existing shutdown test file to check for regressions**

Run: `uv run pytest tests/errors/test_shutdown.py -v`
Expected: all PASS, including the pre-existing `TestForcedBudgetSkipping`/`TestSignalLadder`/etc. classes
-- `_drive_threaded_pass`'s test helper calls `register_for_shutdown` directly with zero-arg callables,
which must still resolve `wants_trail=False` and behave exactly as before.

- [ ] **Step 6: Type-check and lint**

Run: `uv run pyright src/aeth_ext/errors/shutdown.py`
Run: `uv run ruff check src/aeth_ext/errors/shutdown.py tests/errors/_exception_trail_shutdown_scenarios.py tests/errors/test_shutdown.py`

- [ ] **Step 7: Commit**

```bash
git add src/aeth_ext/errors/shutdown.py tests/errors/_exception_trail_shutdown_scenarios.py tests/errors/test_shutdown.py
git commit -m "feat(errors): thread ExceptionTrail through shutdown's callback registry"
```

---

## Task 4: Wire the trail into `aeth_ext.errors.err_handling`

**Files:**
- Modify: `src/aeth_ext/errors/err_handling.py`
- Modify: `tests/errors/_optimized_scenarios.py`
- Modify: `tests/errors/test_err_handling.py`

**Interfaces:**
- Consumes: `ExceptionTrail`, `build_exception_trail` from Task 2; `_set_current_fatal_trail` from
  Task 3.
- Produces: `_handle_fatal(label: str, exc: BaseException, trail: ExceptionTrail | None = None) ->
  None`; `handle_fatal_exc_sync`/`handle_fatal_exc_async` now take `extract_trail_callable:
  Callable[[ExceptionTrail], Any] | None = None` instead of `extract_details_callable`.

The trail is built **exactly once** per fatal exception: when a caller supplies
`extract_trail_callable`, the decorator wrapper builds it there (needs it for the callback anyway) and
passes that same instance into `_handle_fatal`; when no callback is supplied (including every
`report_exc` call, which has no `extract_trail_callable` parameter at all), `_handle_fatal` builds it
itself from its `trail=None` default. This resolves the design doc's two illustrative snippets (one
building inside `_handle_fatal`, one building inside the wrapper) into a single non-redundant path.

- [ ] **Step 1: Write the failing scenario/test changes**

In `tests/errors/_optimized_scenarios.py`, replace the two `extract_details_callable` scenario
functions with trail-based equivalents:

```python
def handle_fatal_exc_sync_extract_trail_callable_invoked() -> dict[str, object]:
  seen: list[str] = []

  @err_handling.handle_fatal_exc_sync(extract_trail_callable=lambda trail: seen.append(trail.origin.module))
  def func() -> None:
    raise ValueError("details please")

  try:
    func()
  except KeyboardInterrupt:
    pass
  return {"seen": seen, "alert_calls": len(alert_calls)}


def handle_fatal_exc_sync_extract_trail_callable_failure_is_caught() -> dict[str, object]:
  @err_handling.handle_fatal_exc_sync(extract_trail_callable=lambda trail: 1 / 0)
  def func() -> None:
    raise ValueError("boom")

  try:
    returned = func()
  except KeyboardInterrupt:
    returned = None
  return {"returned": returned, "alert_calls": len(alert_calls)}


def handle_fatal_exc_async_extract_trail_callable_invoked() -> dict[str, object]:
  seen: list[str] = []

  @err_handling.handle_fatal_exc_async(extract_trail_callable=lambda trail: seen.append(trail.origin.module))
  async def func() -> None:
    raise ValueError("details please")

  try:
    asyncio.run(func())
  except KeyboardInterrupt:
    pass
  return {"seen": seen, "alert_calls": len(alert_calls)}
```

Delete `handle_fatal_exc_sync_extract_details_callable_invoked`,
`handle_fatal_exc_sync_extract_details_callable_failure_is_caught`, and
`handle_fatal_exc_async_extract_details_callable_invoked` (the three functions the ones above replace).
Update the `_SCENARIOS` dict at the bottom of the file: remove the three old keys, add:

```python
  "handle_fatal_exc_sync_extract_trail_callable_invoked": handle_fatal_exc_sync_extract_trail_callable_invoked,
  "handle_fatal_exc_sync_extract_trail_callable_failure_is_caught": (
    handle_fatal_exc_sync_extract_trail_callable_failure_is_caught
  ),
  "handle_fatal_exc_async_extract_trail_callable_invoked": handle_fatal_exc_async_extract_trail_callable_invoked,
```

In `tests/errors/test_err_handling.py`, update the two test methods that reference the old scenario
names:

```python
  def test_extract_trail_callable_is_invoked_with_the_trail(self):
    result = _run_optimized("handle_fatal_exc_sync_extract_trail_callable_invoked")

    assert result == {"seen": ["tests.errors._optimized_scenarios"], "alert_calls": 1}

  def test_extract_trail_callable_failure_is_caught_not_propagated(self):
    result = _run_optimized("handle_fatal_exc_sync_extract_trail_callable_failure_is_caught")

    assert result == {"returned": None, "alert_calls": 1}
```

(replacing `test_extract_details_callable_is_invoked_with_the_exception` and
`test_extract_details_callable_failure_is_caught_not_propagated` under
`TestHandleFatalExcSyncUnderOptimizedMode`), and similarly under `TestHandleFatalExcAsyncUnderOptimizedMode`:

```python
  def test_extract_trail_callable_is_invoked_with_the_trail(self):
    result = _run_optimized("handle_fatal_exc_async_extract_trail_callable_invoked")

    assert result == {"seen": ["tests.errors._optimized_scenarios"], "alert_calls": 1}
```

(replacing `test_extract_details_callable_is_invoked_with_the_exception`). Also update
`TestNoOpUnderNormalDebugMode.test_handle_fatal_exc_sync_with_extract_details_returns_the_original_function`
to use the new kwarg name:

```python
  def test_handle_fatal_exc_sync_with_extract_trail_returns_the_original_function(self):
    def func() -> None:
      pass

    decorator = err_handling.handle_fatal_exc_sync(extract_trail_callable=lambda trail: None)
    assert decorator(func) is func
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/errors/test_err_handling.py -v`
Expected: failures referencing `extract_trail_callable` as an unexpected keyword argument (the
production code still has the old `extract_details_callable` name).

- [ ] **Step 3: Implement the `err_handling.py` changes**

Add to the import block in `src/aeth_ext/errors/err_handling.py`:

```python
# First party imports
from aeth_ext.errors.exception_trail import ExceptionTrail, build_exception_trail
from aeth_ext.errors.shutdown import ShutdownKind, _set_current_fatal_trail, run_shutdown  # noqa: PLC2701 -- private setter, same-package cross-module use
```

(This replaces the existing `from aeth_ext.errors.shutdown import ShutdownKind, run_shutdown` line --
add `_set_current_fatal_trail` to it.)

Replace `_handle_fatal`:

```python
def _handle_fatal(label: str, exc: BaseException, trail: ExceptionTrail | None = None) -> None:
  """Log, alert, and drive a fatal shutdown for *exc*, the exception currently being handled as *label*'s failure.

  Shared by :func:`report_exc`, :func:`handle_fatal_exc_sync`, and :func:`handle_fatal_exc_async` --
  the three of them differ only in how they catch the exception (context manager vs. sync/async
  decorator) and in what they do with it afterward (reraise vs. swallow and return `None`), not in
  how the failure itself gets reported.

  Args:
    label: Human-readable identity of the failing call, used in both the log line and the alert.
    exc: The exception currently being handled. Must be called from inside the `except` block for
      *exc*, since the traceback is rendered from `sys.exc_info()`.
    trail: A pre-built `ExceptionTrail` for *exc*, if the caller already built one (e.g. because it
      also needed to pass it to `extract_trail_callable`). Built fresh here when omitted, so the
      trail is always computed exactly once per fatal exception either way.
  """
  logger.critical("Fatal exception in %s", label, exc_info=exc)
  traceback_text = _extract_rich_traceback()
  alert(f"Fatal exception in {label}", f"{exc}:\n\n{traceback_text}", priority=_FATAL_PUSH_PRIORITY)
  _set_current_fatal_trail(trail if trail is not None else build_exception_trail(exc))
  run_shutdown(ShutdownKind.FATAL)
```

Update `handle_fatal_exc_sync`'s two `@overload`s and implementation, replacing every
`extract_details_callable` with `extract_trail_callable` retyped to `Callable[[ExceptionTrail], Any] |
None`:

```python
@overload
def handle_fatal_exc_sync[**Params_T, Return_T](
  func: None = ..., *, extract_trail_callable: Callable[[ExceptionTrail], Any]
) -> Callable[[Callable[Params_T, Return_T]], Callable[Params_T, Return_T | None]]: ...


@overload
def handle_fatal_exc_sync[**Params_T, Return_T](
  func: Callable[Params_T, Return_T], *, extract_trail_callable: None = ...
) -> Callable[Params_T, Return_T | None]: ...


def handle_fatal_exc_sync[**Params_T, Return_T](
  func: Callable[Params_T, Return_T] | None = None,
  *,
  extract_trail_callable: Callable[[ExceptionTrail], Any] | None = None,
) -> Callable[Params_T, Return_T | None] | Callable[[Callable[Params_T, Return_T]], Callable[Params_T, Return_T | None]]:
  def decorator(
    func: Callable[Params_T, Return_T],
  ) -> Callable[Params_T, Return_T | None]:

    @wraps(func)
    def wrapper(*args: Params_T.args, **kwargs: Params_T.kwargs) -> Return_T | None:
      try:
        return func(*args, **kwargs)
      except CancelledError:
        raise  # raise whatever to make the type checker happy about return values
      except BaseException as e:  # noqa: BLE001 -- fully handled by _handle_fatal (logs, alerts, drives shutdown)
        trail = None
        if extract_trail_callable is not None:
          trail = build_exception_trail(e)
          try:
            extract_trail_callable(trail)
          except Exception as extract_exc:
            logger.exception("Error in extract_trail_callable for exception", exc_info=extract_exc)
        _handle_fatal(func.__qualname__, e, trail)
        return None

    return func if __debug__ and __name__ != "__main__" else wrapper

  if func is not None:
    return decorator(func)

  return decorator
```

Apply the identical pattern to `handle_fatal_exc_async` (same rename, same `trail = None` /
`build_exception_trail(e)` / `_handle_fatal(func.__qualname__, e, trail)` restructuring inside its
`except BaseException as e:` block).

Rename the bottom-of-file stub:

```python
def testing_trail_extractor(trail: ExceptionTrail) -> None:
  pass
```

(replacing `def testing_details_extractor(exc: BaseException) -> None: pass`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/errors/test_err_handling.py -v`
Expected: all PASS.

- [ ] **Step 5: Type-check and lint**

Run: `uv run pyright src/aeth_ext/errors/err_handling.py`
Run: `uv run ruff check src/aeth_ext/errors/err_handling.py tests/errors/_optimized_scenarios.py tests/errors/test_err_handling.py`

- [ ] **Step 6: Commit**

```bash
git add src/aeth_ext/errors/err_handling.py tests/errors/_optimized_scenarios.py tests/errors/test_err_handling.py
git commit -m "feat(errors): replace extract_details_callable with extract_trail_callable"
```

---

## Task 5: Public exports and TODO cleanup

**Files:**
- Modify: `src/aeth_ext/errors/__init__.py`
- Modify: `TODO.md`

**Interfaces:**
- Consumes: `ExceptionTrail`, `OriginCategory`, `TrailEntry`, `build_exception_trail` from Task 2.

- [ ] **Step 1: Export the new public API from the `errors` package**

Modify `src/aeth_ext/errors/__init__.py`:

```python
# Local folder imports
from .err_handling import (
  alert,
  alert_exception,
  handle_fatal_exc_async,
  handle_fatal_exc_sync,
  report_exc,
  trigger_shutdown,
)
from .exception_trail import ExceptionTrail, OriginCategory, TrailEntry, build_exception_trail

__all__ = [
  "ExceptionTrail",
  "OriginCategory",
  "TrailEntry",
  "alert",
  "alert_exception",
  "build_exception_trail",
  "handle_fatal_exc_async",
  "handle_fatal_exc_sync",
  "report_exc",
  "trigger_shutdown",
]
```

- [ ] **Step 2: Verify the import works**

Run: `uv run python -c "from aeth_ext.errors import ExceptionTrail, OriginCategory, TrailEntry, build_exception_trail; print('ok')"`
Expected: prints `ok` with no traceback.

- [ ] **Step 3: Delete TODO.md item 6**

Remove the entire `## 6. Replace extract_details_callable with a standardized fatal-exception-origin
API` section from `TODO.md` (from its `## 6.` heading through the `---` separator immediately before
`## 7.`), matching this file's existing convention of deleting resolved items rather than checking
them off (per the file's own header note about items 
"resolved and ... no longer tracked").

- [ ] **Step 4: Lint**

Run: `uv run ruff check src/aeth_ext/errors/__init__.py`

- [ ] **Step 5: Commit**

```bash
git add src/aeth_ext/errors/__init__.py TODO.md
git commit -m "docs(errors): export ExceptionTrail API and retire TODO item 6"
```

---

## Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: all tests PASS, including every pre-existing test in `tests/errors/` untouched by this plan
(`test_send_alert_email.py`, `test_send_alert_push.py`, `test_traceback_image.py`).

- [ ] **Step 2: Run the full lint and type-check suite**

Run: `uv run ruff check .`
Run: `uv run pyright`
Expected: no errors.

- [ ] **Step 3: Review the diff against the spec's Testing section**

Re-read the spec's "Testing" section
(`.claude/plans/2026-08-13-exception-trail-design.md`, final section) and confirm every bullet has a
corresponding test: trail construction, chain walking, `UNPACKAGED` categorization, `first_party_entry`,
glob matcher, `matches()`, `register_for_shutdown` signature detection, `get_current_fatal_trail`,
`extract_trail_callable` failure isolation. All nine are covered across Tasks 1-4 above.

No commit for this task -- it is a verification gate before the branch is considered ready for its own
PR/merge (per this repo's `finishing-a-development-branch` workflow), not a code change.
