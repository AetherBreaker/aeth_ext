"""Tests for `aeth_ext.logging.config.loader` and the packaged default fragments."""

# Standard library imports
import sys
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest

# First party imports
from aeth_ext.logging.config import loader, runtime_registry
from aeth_ext.logging.config.models import LoggingConfigModel
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

ALL_FRAGMENTS = (
  "main_base",
  "file_daily",
  "file_per_run",
  "console_rich",
  "console_plain",
  "async_queue",
  "worker",
  "log_server_root",
  "server_hierarchy_daily",
  "server_hierarchy_per_run",
  "remote_daily",
  "remote_per_run",
  "socket_client",
)

# Fragment combinations assembled by aeth_ext.logging.setup entry points.
STANDALONE_COMBOS = {
  "main daily rich": ("main_base", "file_daily", "console_rich"),
  "main per_run plain": ("main_base", "file_per_run", "console_plain"),
  "main daily rich async": ("main_base", "file_daily", "console_rich", "async_queue"),
  "main per_run no console async": ("main_base", "file_per_run", "async_queue"),
  "worker": ("worker",),
  "log server root": ("log_server_root",),
  "server hierarchy daily": ("server_hierarchy_daily",),
  "server hierarchy per_run": ("server_hierarchy_per_run",),
  "remote daily": ("remote_daily",),
  "remote per_run": ("remote_per_run",),
  "socket client": ("socket_client",),
  "socket client testing": ("main_base", "file_daily", "console_rich", "socket_client", "async_queue"),
}


@pytest.fixture
def _no_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  """Point every override-search location at empty directories."""
  empty = tmp_path / "empty"
  empty.mkdir()
  monkeypatch.setattr(BaseSettings.get_settings(), "logging_config_loc", None)
  monkeypatch.setattr(sys.modules["__main__"], "__file__", str(empty / "main.py"), raising=False)
  monkeypatch.chdir(empty)


class TestLoadPackagedFragment:
  @pytest.mark.parametrize("name", ALL_FRAGMENTS)
  def test_all_fragments_load(self, name: str):
    fragment = loader.load_packaged_fragment(name)
    assert isinstance(fragment, dict) and fragment

  def test_missing_fragment_raises(self):
    with pytest.raises(ValueError, match="No packaged logging-config fragment named 'nope'"):
      loader.load_packaged_fragment("nope")


class TestAssembleDefaultConfig:
  @pytest.mark.parametrize("names", STANDALONE_COMBOS.values(), ids=STANDALONE_COMBOS.keys())
  def test_standalone_combos_validate(self, names: tuple[str, ...]):
    config = loader.assemble_default_config(*names)
    LoggingConfigModel.model_validate(config)

  def test_console_fragment_appends_to_root_handlers(self):
    config = loader.assemble_default_config("main_base", "file_daily", "console_rich")
    assert config["root"]["handlers"] == ["debug_file", "info_file", "console"]

  def test_async_queue_replaces_root_handlers_with_runtime_ref(self):
    config = loader.assemble_default_config("main_base", "file_daily", "console_rich", "async_queue")
    assert config["root"]["handlers"] == "runtime://root_handler_names"
    assert config["root"]["level"] == "runtime://root_level"
    assert config["handlers"]["queue_catchall"]["handlers"] == "runtime://queued_handler_names"

  def test_socket_client_deep_merges_into_main(self):
    config = loader.assemble_default_config("main_base", "file_daily", "console_rich", "socket_client")
    assert config["root"]["handlers"] == ["debug_file", "info_file", "console", "socket"]
    assert "socket" in config["handlers"]

  def test_file_fragments_share_handler_names(self):
    daily = loader.assemble_default_config("main_base", "file_daily")
    per_run = loader.assemble_default_config("main_base", "file_per_run")
    assert set(daily["handlers"]) == set(per_run["handlers"]) == {"debug_file", "info_file"}
    assert "class" in daily["handlers"]["debug_file"]
    assert "()" in per_run["handlers"]["debug_file"]

  def test_no_merge_markers_survive(self):
    config = loader.assemble_default_config("main_base", "file_daily", "console_rich", "async_queue")

    def find_markers(obj: Any) -> bool:
      if isinstance(obj, dict):
        return "__merge__" in obj or any(find_markers(v) for v in obj.values())
      return False

    assert not find_markers(config)


class TestFindOverrideConfig:
  def test_returns_none_when_nothing_found(self, _no_override: None):
    assert loader.find_override_config() is None

  def test_settings_loc_as_file(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override = tmp_path / "custom_name.toml"
    override.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(BaseSettings.get_settings(), "logging_config_loc", override)
    assert loader.find_override_config() == override

  def test_settings_loc_as_directory(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    override = cfg_dir / loader.DEFAULT_OVERRIDE_FILENAME
    override.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(BaseSettings.get_settings(), "logging_config_loc", cfg_dir)
    assert loader.find_override_config() == override

  def test_entrypoint_dir_fallback(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    main_dir = tmp_path / "app"
    main_dir.mkdir()
    override = main_dir / loader.DEFAULT_OVERRIDE_FILENAME
    override.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(main_dir / "main.py"), raising=False)
    assert loader.find_override_config() == override

  def test_cwd_fallback(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    override = work_dir / loader.DEFAULT_OVERRIDE_FILENAME
    override.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.chdir(work_dir)
    assert loader.find_override_config() == override

  def test_settings_loc_wins_over_entrypoint_and_cwd(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings_file = tmp_path / "settings_override.toml"
    settings_file.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(BaseSettings.get_settings(), "logging_config_loc", settings_file)

    main_dir = tmp_path / "app"
    main_dir.mkdir()
    (main_dir / loader.DEFAULT_OVERRIDE_FILENAME).write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(main_dir / "main.py"), raising=False)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / loader.DEFAULT_OVERRIDE_FILENAME).write_text("version = 1\n", encoding="utf-8")
    monkeypatch.chdir(work_dir)

    assert loader.find_override_config() == settings_file

  def test_entrypoint_wins_over_cwd(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    main_dir = tmp_path / "app"
    main_dir.mkdir()
    entry_override = main_dir / loader.DEFAULT_OVERRIDE_FILENAME
    entry_override.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(main_dir / "main.py"), raising=False)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / loader.DEFAULT_OVERRIDE_FILENAME).write_text("version = 1\n", encoding="utf-8")
    monkeypatch.chdir(work_dir)

    assert loader.find_override_config() == entry_override


class TestLoadEffectiveConfig:
  def test_no_override_returns_assembled_default(self, _no_override: None):
    config = loader.load_effective_config(("main_base", "file_daily"))
    assert config == loader.assemble_default_config("main_base", "file_daily")

  def test_replace_mode_swaps_config_wholesale(self, _no_override: None, tmp_path: Path):
    override = tmp_path / "override.toml"
    override.write_text('version = 1\n\n[root]\nlevel = "WARNING"\n', encoding="utf-8")
    config = loader.load_effective_config(("main_base", "file_daily"), override_path=override)
    assert config == {"version": 1, "root": {"level": "WARNING"}}

  def test_replace_mode_strips_markers(self, _no_override: None, tmp_path: Path):
    override = tmp_path / "override.toml"
    override.write_text('version = 1\n\n[root]\n__merge__ = "deep"\nlevel = "WARNING"\n', encoding="utf-8")
    config = loader.load_effective_config(("main_base",), override_path=override)
    assert config["root"] == {"level": "WARNING"}

  def test_merge_mode_merges_onto_default(self, _no_override: None, tmp_path: Path):
    override = tmp_path / "override.toml"
    override.write_text('[root]\n__merge__ = "deep"\nlevel = "WARNING"\n', encoding="utf-8")
    config = loader.load_effective_config(("main_base", "file_daily"), override_mode="merge", override_path=override)
    assert config["root"]["level"] == "WARNING"
    assert config["root"]["handlers"] == ["debug_file", "info_file"]
    assert set(config["handlers"]) == {"debug_file", "info_file"}

  def test_discovered_override_used_when_no_explicit_path(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    override = work_dir / loader.DEFAULT_OVERRIDE_FILENAME
    override.write_text('version = 1\n\n[root]\nlevel = "ERROR"\n', encoding="utf-8")
    monkeypatch.chdir(work_dir)
    config = loader.load_effective_config(("main_base",))
    assert config == {"version": 1, "root": {"level": "ERROR"}}

  def test_override_filename_selects_alternate_file(self, _no_override: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / loader.DEFAULT_OVERRIDE_FILENAME).write_text('version = 1\n\n[root]\nlevel = "ERROR"\n', encoding="utf-8")
    (work_dir / "logging_config_socket.toml").write_text('version = 1\n\n[root]\nlevel = "CRITICAL"\n', encoding="utf-8")
    monkeypatch.chdir(work_dir)
    config = loader.load_effective_config(("main_base",), override_filename="logging_config_socket.toml")
    assert config == {"version": 1, "root": {"level": "CRITICAL"}}


class TestPreResolve:
  def test_resolves_client_side_prefixes(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PRE_RESOLVE_TEST_VAR", "from-env")
    sentinel = object()
    with runtime_registry.temporarily_registered(pre_resolve_obj=sentinel):
      config = {
        "version": 1,
        "runtime_value": "runtime://pre_resolve_obj",
        "env_value": "env://PRE_RESOLVE_TEST_VAR",
      }

      resolved = loader.pre_resolve(config)

    assert resolved["runtime_value"] is sentinel
    assert resolved["env_value"] == "from-env"

  def test_server_side_prefixes_left_untouched(self):
    config = {
      "version": 1,
      "log_path": "logdir://app.log",
      "cfg_ref": "cfg://handlers.file",
      "ext_ref": "ext://logging.NullHandler",
    }

    resolved = loader.pre_resolve(config)

    assert resolved["log_path"] == "logdir://app.log"
    assert resolved["cfg_ref"] == "cfg://handlers.file"
    assert resolved["ext_ref"] == "ext://logging.NullHandler"

  def test_recurses_into_nested_containers(self):
    sentinel = object()
    with runtime_registry.temporarily_registered(pre_resolve_nested=sentinel):
      config = {
        "version": 1,
        "handlers": {"h": {"queue": "runtime://pre_resolve_nested", "args": ("runtime://pre_resolve_nested", "plain")}},
        "listed": ["runtime://pre_resolve_nested"],
      }

      resolved = loader.pre_resolve(config)

    assert resolved["handlers"]["h"]["queue"] is sentinel
    # Tuples come back as lists (matching the configurator's own behaviour).
    assert resolved["handlers"]["h"]["args"] == [sentinel, "plain"]
    assert resolved["listed"] == [sentinel]

  def test_unresolvable_value_raises(self):
    config = {"version": 1, "value": "runtime://pre_resolve_not_registered"}

    with pytest.raises(ValueError, match="pre_resolve_not_registered"):
      loader.pre_resolve(config)

  def test_returns_a_copy(self):
    config = {"version": 1, "nested": {"plain": "value"}}

    resolved = loader.pre_resolve(config)

    assert resolved == config
    assert resolved is not config
    assert resolved["nested"] is not config["nested"]
