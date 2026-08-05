"""Tests for private-hierarchy configuration and pickled ``definition`` support.

Covers `aeth_ext.logging.config.DictConfigurator`'s private ``manager``/``root``
mode, the ``logdir://`` converter, and cloudpickled ``definition`` entries
(created client-side with `aeth_ext.central_log_server.client.make_definition`).
"""

# Standard library imports
import logging
from typing import TYPE_CHECKING, override

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.client import make_definition
from aeth_ext.logging import config as dc
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


class _MarkerFilter(logging.Filter):
  @override
  def filter(self, record: logging.LogRecord) -> bool:
    record.marker = True
    return True


def _make_formatter() -> logging.Formatter:
  return logging.Formatter("DEF %(message)s")


def _make_filter() -> _MarkerFilter:
  return _MarkerFilter()


def _make_handler() -> logging.NullHandler:
  return logging.NullHandler()


class TestPrivateHierarchy:
  def test_config_lands_in_private_hierarchy_only(self):
    config = {
      "version": 1,
      "handlers": {"phier_private": {"class": "logging.NullHandler"}},
      "loggers": {"phier.child": {"level": "ERROR"}},
      "root": {"level": "INFO", "handlers": ["phier_private"]},
    }

    manager, root = dc.DictConfigurator(config).apply(private=True)

    assert root.level == logging.INFO
    (handler,) = root.handlers
    assert handler.get_name() == "phier_private"
    assert manager.getLogger("phier.child").level == logging.ERROR
    # None of it leaked into the global hierarchy.
    assert logging.getHandlerByName("phier_private") is None
    assert "phier.child" not in logging.Logger.manager.loggerDict
    assert logging.root.handlers != [handler]

  def test_private_handler_names_may_shadow_global_ones(self):
    global_handler = logging.NullHandler()
    global_handler.name = "phier_shared"
    config = {
      "version": 1,
      "handlers": {"phier_shared": {"class": "logging.NullHandler"}},
      "root": {"handlers": ["phier_shared"]},
    }

    _manager, root = dc.DictConfigurator(config).apply(private=True)

    (private_handler,) = root.handlers
    assert private_handler.get_name() == "phier_shared"
    assert private_handler is not global_handler
    # The global registry still resolves to the global handler.
    assert logging.getHandlerByName("phier_shared") is global_handler

  def test_private_mode_leaves_global_handlers_attached(self):
    sentinel = logging.NullHandler()
    logging.root.addHandler(sentinel)

    dc.DictConfigurator({"version": 1, "root": {"level": "INFO"}}).apply(private=True)

    # Global application would have cleared existing handlers; private must not.
    assert sentinel in logging.root.handlers


class TestLogdirConverter:
  def test_paths_resolve_beneath_log_dir(self, tmp_path: Path):
    configurator = dc.BaseConfigurator({}, log_dir=tmp_path)

    resolved = configurator.convert("logdir://sub/app.log")

    assert resolved == tmp_path / "sub" / "app.log"
    # D-A3: logdir:// only resolves the path; mkdir happens in one pass
    # during DictConfigurator.apply() (see test_dispatch.py for the e2e case).
    assert not (tmp_path / "sub").exists()
    assert tmp_path / "sub" in configurator._pending_mkdirs  # pyright: ignore[reportPrivateUsage]

  def test_requires_a_log_dir(self):
    configurator = dc.BaseConfigurator({})

    with pytest.raises(ValueError, match="no log_dir was provided"):
      configurator.convert("logdir://app.log")


class TestResolveDefinition:
  def test_round_trips_make_definition(self):
    encoded = make_definition(_make_formatter)

    resolved = dc.BaseConfigurator({}).resolve_definition(encoded)

    assert resolved is _make_formatter
    assert isinstance(resolved(), logging.Formatter)

  def test_gated_behind_settings_flag(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(BaseSettings.get_settings(), "logging_allow_pickled_definitions", False)
    encoded = make_definition(_make_formatter)

    with pytest.raises(ValueError, match="definition' entries are disabled"):
      dc.BaseConfigurator({}).resolve_definition(encoded)


class TestDefinitionKeyInConfigs:
  def test_definition_builds_formatter_filter_and_handler(self):
    config = {
      "version": 1,
      "formatters": {"custom": {"definition": make_definition(_make_formatter)}},
      "filters": {"marker": {"definition": make_definition(_make_filter)}},
      "handlers": {
        "custom": {
          "definition": make_definition(_make_handler),
          "formatter": "custom",
          "filters": ["marker"],
          "level": "INFO",
        },
      },
      "root": {"level": "DEBUG", "handlers": ["custom"]},
    }

    _manager, root = dc.DictConfigurator(config).apply(private=True)

    (handler,) = root.handlers
    assert isinstance(handler, logging.NullHandler)
    assert handler.level == logging.INFO
    assert handler.formatter is not None
    assert handler.formatter._fmt == "DEF %(message)s"
    assert any(isinstance(f, _MarkerFilter) for f in handler.filters)

  def test_rejected_definition_surfaces_as_config_error(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(BaseSettings.get_settings(), "logging_allow_pickled_definitions", False)
    config = {
      "version": 1,
      "formatters": {"custom": {"definition": make_definition(_make_formatter)}},
    }

    with pytest.raises(ValueError, match="Unable to configure formatter 'custom'"):
      dc.DictConfigurator(config).apply(private=True)
