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
    assert re.search("database", "scheduled_invoice_processor.database.orm") is not None  # sanity: substring exists
    assert not _compile_pattern("database").fullmatch("scheduled_invoice_processor.database.orm")
