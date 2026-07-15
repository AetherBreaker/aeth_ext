"""Tests for `aeth_ext.logging.config.merge`."""

# Standard library imports
from typing import Any

# Third party imports
import pytest

# First party imports
from aeth_ext.logging.config.merge import MERGE_MARKER, assemble_configs, merge_configs, strip_merge_markers


class TestStripMergeMarkers:
  def test_removes_top_level_marker(self):
    assert strip_merge_markers({MERGE_MARKER: "deep", "a": 1}) == {"a": 1}

  def test_removes_nested_markers(self):
    cfg = {"root": {MERGE_MARKER: "deep", "level": "INFO", "sub": {MERGE_MARKER: "deep", "x": 1}}}
    assert strip_merge_markers(cfg) == {"root": {"level": "INFO", "sub": {"x": 1}}}

  def test_returns_deep_copy(self):
    cfg = {"handlers": {"h": {"filters": ["f"]}}}
    result = strip_merge_markers(cfg)
    result["handlers"]["h"]["filters"].append("g")
    assert cfg["handlers"]["h"]["filters"] == ["f"]

  def test_non_dict_values_preserved(self):
    cfg = {"version": 1, "names": ["a", "b"]}
    assert strip_merge_markers(cfg) == cfg


class TestMergeConfigsScalars:
  def test_scalar_keys_replaced(self):
    assert merge_configs({"version": 1, "disable_existing_loggers": True}, {"disable_existing_loggers": False}) == {
      "version": 1,
      "disable_existing_loggers": False,
    }

  def test_new_top_level_key_added(self):
    assert merge_configs({"version": 1}, {"incremental": False}) == {"version": 1, "incremental": False}


class TestMergeConfigsSections:
  def test_same_named_entry_replaced_wholesale(self):
    base = {"handlers": {"console": {"class": "logging.StreamHandler", "level": "INFO"}}}
    override = {"handlers": {"console": {"class": "logging.NullHandler"}}}
    merged = merge_configs(base, override)
    assert merged["handlers"]["console"] == {"class": "logging.NullHandler"}

  def test_different_named_entries_coexist(self):
    base = {"handlers": {"a": {"class": "logging.NullHandler"}}}
    override = {"handlers": {"b": {"class": "logging.NullHandler"}}}
    merged = merge_configs(base, override)
    assert set(merged["handlers"]) == {"a", "b"}

  def test_marker_triggers_deep_merge(self):
    base = {"handlers": {"console": {"class": "logging.StreamHandler", "level": "INFO"}}}
    override = {"handlers": {"console": {MERGE_MARKER: "deep", "level": "DEBUG"}}}
    merged = merge_configs(base, override)
    assert merged["handlers"]["console"] == {"class": "logging.StreamHandler", "level": "DEBUG"}

  def test_marker_on_new_entry_is_plain_insert(self):
    override = {"handlers": {"console": {MERGE_MARKER: "deep", "level": "DEBUG"}}}
    merged = merge_configs({}, override)
    assert merged["handlers"]["console"] == {"level": "DEBUG"}

  def test_all_section_keys_supported(self):
    base = {
      "formatters": {"f": {"format": "x"}},
      "filters": {"fl": {"name": "y"}},
      "handlers": {"h": {"class": "logging.NullHandler"}},
      "loggers": {"lg": {"level": "INFO"}},
    }
    override = {
      "formatters": {"f": {"format": "z"}},
      "filters": {"fl2": {"name": "w"}},
      "handlers": {"h": {MERGE_MARKER: "deep", "level": "DEBUG"}},
      "loggers": {"lg": {MERGE_MARKER: "deep", "propagate": False}},
    }
    merged = merge_configs(base, override)
    assert merged["formatters"]["f"] == {"format": "z"}
    assert set(merged["filters"]) == {"fl", "fl2"}
    assert merged["handlers"]["h"] == {"class": "logging.NullHandler", "level": "DEBUG"}
    assert merged["loggers"]["lg"] == {"level": "INFO", "propagate": False}


class TestMergeConfigsRoot:
  def test_root_replaced_without_marker(self):
    base = {"root": {"level": "INFO", "handlers": ["a"]}}
    override = {"root": {"handlers": ["b"]}}
    assert merge_configs(base, override)["root"] == {"handlers": ["b"]}

  def test_root_deep_merged_with_marker(self):
    base = {"root": {"level": "INFO", "handlers": ["a"]}}
    override = {"root": {MERGE_MARKER: "deep", "handlers": ["b"]}}
    assert merge_configs(base, override)["root"] == {"level": "INFO", "handlers": ["a", "b"]}

  def test_root_marker_without_base_is_plain_insert(self):
    override = {"root": {MERGE_MARKER: "deep", "handlers": ["b"]}}
    assert merge_configs({}, override)["root"] == {"handlers": ["b"]}


class TestDeepMergeListSemantics:
  def test_lists_concatenate(self):
    base = {"root": {"handlers": ["a", "b"]}}
    override = {"root": {MERGE_MARKER: "deep", "handlers": ["c"]}}
    assert merge_configs(base, override)["root"]["handlers"] == ["a", "b", "c"]

  def test_list_concat_dedupes(self):
    base = {"root": {"handlers": ["a", "b"]}}
    override = {"root": {MERGE_MARKER: "deep", "handlers": ["b", "c"]}}
    assert merge_configs(base, override)["root"]["handlers"] == ["a", "b", "c"]

  def test_type_mismatch_replaces(self):
    # A converter-protocol string replacing a list is relied upon by the
    # async_queue fragment ("runtime://root_handler_names" over a list).
    base = {"root": {"level": "INFO", "handlers": ["a", "b"]}}
    override = {"root": {MERGE_MARKER: "deep", "handlers": "runtime://root_handler_names"}}
    merged = merge_configs(base, override)
    assert merged["root"] == {"level": "INFO", "handlers": "runtime://root_handler_names"}


class TestMarkerStripping:
  def test_result_never_contains_markers(self):
    base = {"root": {"level": "INFO"}, "handlers": {"h": {"class": "logging.NullHandler"}}}
    override = {
      "root": {MERGE_MARKER: "deep", "handlers": ["h"]},
      "handlers": {"h2": {MERGE_MARKER: "deep", "class": "logging.NullHandler"}},
    }
    merged = merge_configs(base, override)

    def find_markers(obj: Any) -> bool:
      if isinstance(obj, dict):
        return MERGE_MARKER in obj or any(find_markers(v) for v in obj.values())
      return False

    assert not find_markers(merged)

  def test_base_markers_stripped_even_without_override(self):
    base = {"root": {MERGE_MARKER: "deep", "level": "INFO"}}
    assert merge_configs(base, {}) == {"root": {"level": "INFO"}}


class TestAssembleConfigs:
  def test_no_configs_raises(self):
    with pytest.raises(ValueError, match="at least one config"):
      assemble_configs()

  def test_single_config_stripped_copy(self):
    cfg = {"version": 1, "root": {MERGE_MARKER: "deep", "level": "INFO"}}
    result = assemble_configs(cfg)
    assert result == {"version": 1, "root": {"level": "INFO"}}
    result["version"] = 2
    assert cfg["version"] == 1

  def test_left_to_right_merge(self):
    first = {"version": 1, "root": {"level": "INFO", "handlers": []}}
    second = {"root": {MERGE_MARKER: "deep", "handlers": ["a"]}}
    third = {"root": {MERGE_MARKER: "deep", "handlers": ["b"]}}
    assert assemble_configs(first, second, third)["root"] == {"level": "INFO", "handlers": ["a", "b"]}

  def test_later_config_wins_on_conflict(self):
    assert assemble_configs({"version": 1}, {"version": 1, "incremental": True}, {"incremental": False})["incremental"] is False
