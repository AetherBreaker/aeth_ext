"""Tests for `aeth_ext.logging.config` (dict_config, BaseConfigurator, and friends)."""

# Standard library imports
import json
import logging
import logging.handlers

# Third party imports
import pydantic
import pytest

# First party imports
import aeth_ext.logging.config.models
from aeth_ext.logging import config as dc

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
    dc.DictConfigurator(MINIMAL_CONFIG).apply()
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)
    assert logging.root.level == logging.INFO

  def test_full_configure_attaches_handler_to_named_logger(self):
    cfg = _sample_config_with_logger()
    dc.DictConfigurator(cfg).apply()
    logger = logging.getLogger("myapp")
    assert logger.level == logging.DEBUG
    assert logger.propagate is False
    assert len(logger.handlers) == 1

  def test_incremental_updates_handler_level_only(self):
    cfg = _sample_config_with_logger()
    dc.DictConfigurator(cfg).apply()
    handler_before = logging.getLogger("myapp").handlers[0]

    incr_cfg = {
      "version": 1,
      "incremental": True,
      "handlers": {"console": {"level": "WARNING"}},
      "loggers": {"myapp": {"level": "ERROR"}},
    }
    dc.DictConfigurator(incr_cfg).apply()

    assert handler_before.level == logging.WARNING
    assert logging.getLogger("myapp").level == logging.ERROR
    # Handler should not have been removed/replaced.
    assert logging.getLogger("myapp").handlers[0] is handler_before

  def test_disable_existing_loggers_disables_stale_loggers(self):
    logging.getLogger("stale.logger")
    dc.DictConfigurator(MINIMAL_CONFIG).apply()
    assert logging.getLogger("stale.logger").disabled is True

  def test_disable_existing_loggers_false_keeps_enabled(self):
    logging.getLogger("stale.logger2")
    cfg = {**MINIMAL_CONFIG, "disable_existing_loggers": False}
    dc.DictConfigurator(cfg).apply()
    assert logging.getLogger("stale.logger2").disabled is False

  def test_child_logger_of_named_logger_not_disabled(self):
    cfg = _sample_config_with_logger()
    logging.getLogger("myapp.child")
    dc.DictConfigurator(cfg).apply()
    assert logging.getLogger("myapp.child").disabled is False

  def test_accepts_validated_model(self):
    model = aeth_ext.logging.config.models.LoggingConfigModel.model_validate(MINIMAL_CONFIG)
    dc.DictConfigurator(model.model_dump(by_alias=True, exclude_none=True)).apply()
    assert any(isinstance(h, logging.StreamHandler) for h in logging.root.handlers)

  def test_invalid_config_raises_validation_error(self):
    with pytest.raises(pydantic.ValidationError):
      dc.DictConfigurator({"version": 1, "bogus_key": True}).apply()


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
    dc.DictConfigurator(cfg).apply()
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
    dc.DictConfigurator(cfg).apply()
    mem_handler = next(h for h in logging.root.handlers if isinstance(h, logging.handlers.MemoryHandler))
    assert mem_handler.target is not None


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
