"""Tests for `aeth_ext.logging.dict_config`."""

# Standard library imports
import io
import json
import logging
import logging.handlers
import socket
import struct
import time
from typing import TYPE_CHECKING

# Third party imports
import pydantic
import pytest

# First party imports
import aeth_ext.logging.config.models
from aeth_ext.logging import config as dc

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

MINIMAL_CONFIG: dict = {
  "version": 1,
  "formatters": {
    "simple": {"format": "%(levelname)s:%(name)s:%(message)s"},
  },
  "handlers": {
    "console": {
      "class": "logging.StreamHandler",
      "formatter": "simple",
      "level": "INFO",
    },
  },
  "root": {
    "level": "INFO",
    "handlers": ["console"],
  },
}


def _sample_config_with_logger() -> dict:
  cfg = json.loads(json.dumps(MINIMAL_CONFIG))
  cfg["loggers"] = {
    "myapp": {
      "level": "DEBUG",
      "handlers": ["console"],
      "propagate": False,
    },
  }
  return cfg


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
  def test_minimal_config_validates(self):
    aeth_ext.logging.config.models.LoggingConfigModel.model_validate(MINIMAL_CONFIG)

  def test_missing_version_raises(self):
    cfg = {k: v for k, v in MINIMAL_CONFIG.items() if k != "version"}
    with pytest.raises(pydantic.ValidationError):
      aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)

  def test_wrong_version_raises(self):
    cfg = {**MINIMAL_CONFIG, "version": 2}
    with pytest.raises(pydantic.ValidationError):
      aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)

  def test_unknown_top_level_key_raises(self):
    cfg = {**MINIMAL_CONFIG, "not_a_real_key": True}
    with pytest.raises(pydantic.ValidationError):
      aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)

  def test_unknown_key_in_logger_raises(self):
    cfg = _sample_config_with_logger()
    cfg["loggers"]["myapp"]["bogus"] = 1
    with pytest.raises(pydantic.ValidationError):
      aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)

  def test_unknown_key_in_root_raises(self):
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["root"]["bogus"] = 1
    with pytest.raises(pydantic.ValidationError):
      aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)

  def test_unknown_key_in_handler_is_allowed(self):
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["handlers"]["console"]["custom_kwarg"] = "value"
    model = aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)
    assert model.handlers["console"].model_extra is not None
    assert model.handlers["console"].model_extra["custom_kwarg"] == "value"

  def test_unknown_key_in_formatter_is_allowed(self):
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["formatters"]["simple"]["custom_kwarg"] = "value"
    model = aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)
    assert model.formatters["simple"].model_extra is not None

  def test_unknown_key_in_filter_is_allowed(self):
    cfg = json.loads(json.dumps(MINIMAL_CONFIG))
    cfg["filters"] = {"f1": {"name": "x", "custom_kwarg": "value"}}
    model = aeth_ext.logging.config.models.LoggingConfigModel.model_validate(cfg)
    assert model.filters["f1"].model_extra is not None


# ---------------------------------------------------------------------------
# dict_config / DictConfigurator
# ---------------------------------------------------------------------------


class TestDictConfig:
  def test_full_configure_attaches_handler_to_root(self):
    dc.dict_config(MINIMAL_CONFIG)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)
    assert logging.root.level == logging.INFO

  def test_full_configure_attaches_handler_to_named_logger(self):
    cfg = _sample_config_with_logger()
    dc.dict_config(cfg)
    logger = logging.getLogger("myapp")
    assert logger.level == logging.DEBUG
    assert logger.propagate is False
    assert len(logger.handlers) == 1

  def test_incremental_updates_handler_level_only(self):
    cfg = _sample_config_with_logger()
    dc.dict_config(cfg)
    handler_before = logging.getLogger("myapp").handlers[0]

    incr_cfg = {
      "version": 1,
      "incremental": True,
      "handlers": {"console": {"level": "WARNING"}},
      "loggers": {"myapp": {"level": "ERROR"}},
    }
    dc.dict_config(incr_cfg)

    assert handler_before.level == logging.WARNING
    assert logging.getLogger("myapp").level == logging.ERROR
    # Handler should not have been removed/replaced.
    assert logging.getLogger("myapp").handlers[0] is handler_before

  def test_disable_existing_loggers_disables_stale_loggers(self):
    logging.getLogger("stale.logger")
    dc.dict_config(MINIMAL_CONFIG)
    assert logging.getLogger("stale.logger").disabled is True

  def test_disable_existing_loggers_false_keeps_enabled(self):
    logging.getLogger("stale.logger2")
    cfg = {**MINIMAL_CONFIG, "disable_existing_loggers": False}
    dc.dict_config(cfg)
    assert logging.getLogger("stale.logger2").disabled is False

  def test_child_logger_of_named_logger_not_disabled(self):
    cfg = _sample_config_with_logger()
    logging.getLogger("myapp.child")
    dc.dict_config(cfg)
    assert logging.getLogger("myapp.child").disabled is False

  def test_accepts_validated_model(self):
    model = aeth_ext.logging.config.models.LoggingConfigModel.model_validate(MINIMAL_CONFIG)
    dc.dict_config(model)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_invalid_config_raises_validation_error(self):
    with pytest.raises(pydantic.ValidationError):
      dc.dict_config({"version": 1, "bogus_key": True})


# ---------------------------------------------------------------------------
# Handler wiring edge cases
# ---------------------------------------------------------------------------


class TestHandlerWiring:
  def test_memory_handler_with_target(self):
    cfg = {
      "version": 1,
      "handlers": {
        "target": {"class": "logging.StreamHandler"},
        "mem": {
          "class": "logging.handlers.MemoryHandler",
          "capacity": 10,
          "target": "target",
        },
      },
      "root": {"level": "DEBUG", "handlers": ["mem"]},
    }
    dc.dict_config(cfg)
    mem_handler = next(h for h in logging.root.handlers if isinstance(h, logging.handlers.MemoryHandler))
    assert mem_handler.target is not None

  def test_deferred_handler_reference_ordering(self):
    # "mem" (named 'a_mem') references 'z_target' which sorts after it -
    # exercises the retry path since handlers are configured in name order.
    cfg = {
      "version": 1,
      "handlers": {
        "a_mem": {
          "class": "logging.handlers.MemoryHandler",
          "capacity": 10,
          "target": "z_target",
        },
        "z_target": {"class": "logging.StreamHandler"},
      },
      "root": {"level": "DEBUG", "handlers": ["a_mem"]},
    }
    dc.dict_config(cfg)
    mem_handler = next(h for h in logging.root.handlers if isinstance(h, logging.handlers.MemoryHandler))
    assert mem_handler.target is not None


# ---------------------------------------------------------------------------
# json_config
# ---------------------------------------------------------------------------


class TestJsonConfig:
  def test_from_path_string(self, tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(MINIMAL_CONFIG))
    dc.json_config(str(p))
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_from_path_object(self, tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(MINIMAL_CONFIG))
    dc.json_config(p)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_from_file_like_object(self):
    dc.json_config(io.StringIO(json.dumps(MINIMAL_CONFIG)))
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)


# ---------------------------------------------------------------------------
# YAML / TOML helpers
# ---------------------------------------------------------------------------

YAML_BACKENDS = []
try:
  # Third party imports
  import yaml12  # noqa: F401

  YAML_BACKENDS.append("yaml12")
except ImportError:
  pass
try:
  # Third party imports
  import yaml  # noqa: F401

  YAML_BACKENDS.append("pyyaml")
except ImportError:
  pass

MINIMAL_CONFIG_YAML = """
version: 1
formatters:
  simple:
    format: "%(levelname)s:%(name)s:%(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: simple
    level: INFO
root:
  level: INFO
  handlers: [console]
"""

MINIMAL_CONFIG_TOML = """
version = 1

[formatters.simple]
format = "%(levelname)s:%(name)s:%(message)s"

[handlers.console]
class = "logging.StreamHandler"
formatter = "simple"
level = "INFO"

[root]
level = "INFO"
handlers = ["console"]
"""


class TestYamlToml:
  @pytest.mark.parametrize(
    "backend", YAML_BACKENDS or pytest.param("none", marks=pytest.mark.skip(reason="no yaml backend installed"))
  )
  def test_yaml_to_json_matches_parsed_structure(self, backend: str, monkeypatch: pytest.MonkeyPatch):
    if backend == "pyyaml":
      monkeypatch.setitem(__import__("sys").modules, "yaml12", None)
    result = dc.yaml_to_json(MINIMAL_CONFIG_YAML)
    assert json.loads(result) == MINIMAL_CONFIG

  def test_yaml_config_applies_config(self):
    dc.yaml_config(MINIMAL_CONFIG_YAML)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_toml_to_json_matches_parsed_structure(self):
    result = dc.toml_to_json(MINIMAL_CONFIG_TOML)
    assert json.loads(result) == MINIMAL_CONFIG

  def test_toml_config_applies_config(self):
    dc.toml_config(MINIMAL_CONFIG_TOML)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_yaml_config_from_path(self, tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(MINIMAL_CONFIG_YAML)
    dc.yaml_config(p)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_toml_config_from_path(self, tmp_path: Path):
    p = tmp_path / "cfg.toml"
    p.write_text(MINIMAL_CONFIG_TOML)
    dc.toml_config(p)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)


# ---------------------------------------------------------------------------
# Removed INI pipeline (regression guard)
# ---------------------------------------------------------------------------


class TestIniRemoved:
  @pytest.mark.parametrize(
    "attr",
    [
      "file_config",
      "_create_formatters",
      "_install_handlers",
      "_configure_root_logger_from_config",
      "_configure_named_logger",
      "_install_loggers",
      "_strip_spaces",
    ],
  )
  def test_ini_helpers_removed(self, attr: str):
    assert not hasattr(dc, attr)


# ---------------------------------------------------------------------------
# Socket listener (JSON-only)
# ---------------------------------------------------------------------------


class TestSocketConfigChunk:
  def test_valid_json_chunk_applies_config(self):
    dc._process_socket_config_chunk(json.dumps(MINIMAL_CONFIG).encode("utf-8"))  # pyright: ignore[reportPrivateUsage]
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_ini_formatted_chunk_does_not_apply(self):
    ini_text = "[loggers]\nkeys=root\n[handlers]\nkeys=\n[formatters]\nkeys=\n[logger_root]\nlevel=INFO\nhandlers=\n"
    before = logging.root.handlers[:]
    dc._process_socket_config_chunk(ini_text.encode("utf-8"))  # pyright: ignore[reportPrivateUsage]
    # No new StreamHandler should have been installed via this malformed payload.
    assert logging.root.handlers == before

  def test_malformed_json_chunk_is_caught(self):
    # Should not raise.
    dc._process_socket_config_chunk(b"{not valid json")  # pyright: ignore[reportPrivateUsage]


class TestReceiveLengthPrefixedChunk:
  def test_frames_a_chunk(self):
    payload = b"hello world"
    data = struct.pack(">L", len(payload)) + payload

    class _FakeConn:
      def __init__(self, data: bytes) -> None:
        self._data = data

      def recv(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    result = dc._receive_length_prefixed_chunk(_FakeConn(data))  # type: ignore[arg-type]
    assert result == payload

  def test_returns_none_on_short_prefix(self):
    class _FakeConn:
      def recv(self, n: int) -> bytes:
        return b"ab"

    assert dc._receive_length_prefixed_chunk(_FakeConn()) is None  # type: ignore[arg-type]


class TestListen:
  def test_listen_applies_config_over_socket(self):
    server_thread = dc.listen(port=0)
    server_thread.start()
    server_thread.ready.wait(timeout=5)
    port = server_thread.port

    try:
      payload = json.dumps(MINIMAL_CONFIG).encode("utf-8")
      with socket.create_connection(("localhost", port), timeout=5) as sock:
        sock.sendall(struct.pack(">L", len(payload)) + payload)
        # Give the server a moment to process and close cleanly.
        time.sleep(0.2)
    finally:
      dc.stop_listening()
      server_thread.join(timeout=5)

    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)
