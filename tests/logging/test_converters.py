"""Tests for the `setting://`, `runtime://`, and `env://` value converters."""

# Standard library imports
import logging
from typing import Any

# Third party imports
import pytest

# First party imports
from aeth_ext.logging import config as dc
from aeth_ext.logging.config import runtime_registry
from aeth_ext.settings import BaseSettings

MINIMAL_CONFIG: dict = {
  "version": 1,
  "root": {"level": "INFO"},
}

_captured_kwargs: dict = {}


def _capturing_handler_factory(**kwargs: Any) -> logging.Handler:
  """Handler factory that records the (already converted) kwargs it receives."""
  _captured_kwargs.clear()
  _captured_kwargs.update(kwargs)
  return logging.NullHandler()


@pytest.fixture
def configurator() -> dc.DictConfigurator:
  return dc.DictConfigurator(MINIMAL_CONFIG)


class TestRuntimeConvert:
  def test_resolves_registered_object(self, configurator: dc.DictConfigurator):
    sentinel = object()
    runtime_registry.register("my_obj", sentinel)
    assert configurator.convert("runtime://my_obj") is sentinel

  def test_missing_name_raises_value_error(self, configurator: dc.DictConfigurator):
    with pytest.raises(ValueError, match="No runtime object registered under 'missing'"):
      configurator.convert("runtime://missing")

  def test_non_string_values_pass_through(self, configurator: dc.DictConfigurator):
    # Scalars pass through untouched; containers are wrapped but keep their contents.
    scalar = "no-protocol-prefix"
    assert configurator.convert(scalar) == scalar
    assert configurator.convert(["not", "a", "string"]) == ["not", "a", "string"]


class TestSettingConvert:
  def test_resolves_settings_attribute(self, configurator: dc.DictConfigurator):
    expected = BaseSettings.get_settings().log_conn_port
    assert configurator.convert("setting://log_conn_port") == expected

  def test_resolves_dotted_path(self, configurator: dc.DictConfigurator):
    expected = BaseSettings.get_settings().log_loc_folder.name
    assert configurator.convert("setting://log_loc_folder.name") == expected

  def test_missing_attribute_raises_value_error(self, configurator: dc.DictConfigurator):
    with pytest.raises(ValueError, match="no attribute 'not_a_setting'"):
      configurator.convert("setting://not_a_setting")


class TestEnvConvert:
  def test_resolves_environment_variable(self, configurator: dc.DictConfigurator, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AETH_TEST_ENV_VAR", "hello")
    assert configurator.convert("env://AETH_TEST_ENV_VAR") == "hello"

  def test_unset_variable_raises_value_error(self, configurator: dc.DictConfigurator, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AETH_TEST_UNSET_VAR", raising=False)
    with pytest.raises(ValueError, match="environment variable 'AETH_TEST_UNSET_VAR' is not set"):
      configurator.convert("env://AETH_TEST_UNSET_VAR")


class TestConvertersEndToEnd:
  def test_handler_kwargs_are_converted_before_factory_call(self, monkeypatch: pytest.MonkeyPatch):
    sentinel = object()
    runtime_registry.register("e2e_obj", sentinel)
    monkeypatch.setenv("AETH_E2E_ENV", "env-value")

    config = {
      "version": 1,
      "handlers": {
        "captured": {
          "()": f"{__name__}._capturing_handler_factory",
          "runtime_kwarg": "runtime://e2e_obj",
          "env_kwarg": "env://AETH_E2E_ENV",
          "setting_kwarg": "setting://log_conn_port",
        },
      },
      "root": {"level": "INFO", "handlers": ["captured"]},
    }
    dc.dict_config(config)

    assert _captured_kwargs["runtime_kwarg"] is sentinel
    assert _captured_kwargs["env_kwarg"] == "env-value"
    assert _captured_kwargs["setting_kwarg"] == BaseSettings.get_settings().log_conn_port

  def test_unresolvable_runtime_ref_fails_configuration(self):
    config = {
      "version": 1,
      "handlers": {
        "captured": {
          "()": f"{__name__}._capturing_handler_factory",
          "runtime_kwarg": "runtime://definitely_not_registered",
        },
      },
      "root": {"level": "INFO", "handlers": ["captured"]},
    }
    with pytest.raises(ValueError):
      dc.dict_config(config)
