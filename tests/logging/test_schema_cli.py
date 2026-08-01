"""Tests for `aeth_ext.logging.schema_cli` (the `aeth-ext-logging-schema` CLI).

Every test passes an explicit `output` inside `tmp_path` rather than relying
on the module's own default (`schema_output_loc`, the real checked-in schema
file) -- that default is bound once at import time, so it always resolves to
the real repo path regardless of monkeypatching, and must never be
overwritten by a test run.
"""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import orjson

# First party imports
from aeth_ext.logging import schema_cli as schema_cli_module
from aeth_ext.logging.config.models import LoggingConfigModel
from aeth_ext.logging.schema_cli import cli

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

  # Third party imports
  import pytest


class TestOutputPathResolution:
  def test_a_path_with_a_suffix_is_written_directly_even_if_it_does_not_exist_yet(self, tmp_path: Path):
    target = tmp_path / "my_schema.json"

    cli(output=target)

    assert target.is_file()
    assert not (tmp_path / schema_cli_module.schema_output_loc.name).exists()

  def test_an_existing_directory_gets_the_default_filename_appended(self, tmp_path: Path):
    directory = tmp_path / "out"
    directory.mkdir()

    cli(output=directory)

    assert (directory / schema_cli_module.schema_output_loc.name).is_file()
    assert not directory.with_suffix(".json").exists()

  def test_a_suffixless_path_whose_directory_is_missing_gets_created(self, tmp_path: Path):
    target = tmp_path / "does_not_exist_yet"

    cli(output=target)

    assert (target / schema_cli_module.schema_output_loc.name).is_file()

  def test_an_existing_directory_with_a_dotted_name_still_gets_the_default_filename_appended(self, tmp_path: Path):
    directory = tmp_path / "schemas.json"
    directory.mkdir()

    cli(output=directory)

    assert (directory / schema_cli_module.schema_output_loc.name).is_file()
    assert directory.is_dir()


class TestSchemaContent:
  def test_written_schema_round_trips_and_matches_the_model(self, tmp_path: Path):
    target = tmp_path / "schema.json"

    cli(output=target)

    written = orjson.loads(target.read_bytes())
    assert written == LoggingConfigModel.model_json_schema()

  def test_indent_false_produces_more_compact_output_than_indent_true(self, tmp_path: Path):
    indented = tmp_path / "indented.json"
    compact = tmp_path / "compact.json"

    cli(output=indented, indent=True)
    cli(output=compact, indent=False)

    assert len(compact.read_bytes()) < len(indented.read_bytes())
    assert orjson.loads(compact.read_bytes()) == orjson.loads(indented.read_bytes())

  def test_prints_confirmation_with_the_resolved_output_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "schema.json"

    cli(output=target)

    assert str(target) in capsys.readouterr().out
