"""Tests for `aeth_ext.central_log_server.client.config_provider`.

`_find_package_file`/`_resolve_target` are exercised directly in-process
(fast); `query_logging_configs` itself always spawns a real subprocess by
design (see the module's own docstring: importing a child package's config
function must never register loggers into the current process), so its own
tests are real, if slower, end-to-end runs against the fixture modules in
`tests/central_log_server/_config_provider_fixtures/`.
"""

# Standard library imports
from typing import Any

# Third party imports
import pytest

# First party imports
from aeth_ext.central_log_server.client.config_provider import (
  _find_package_file,  # pyright: ignore[reportPrivateUsage]
  _resolve_target,  # pyright: ignore[reportPrivateUsage]
  query_logging_configs,
)

_FIXTURES_PKG = "tests.central_log_server._config_provider_fixtures"
_TIMEOUT = 15.0


class TestFindPackageFile:
  def test_returns_the_real_module_file_for_an_importable_module(self):
    result = _find_package_file(f"{_FIXTURES_PKG}.with_default_func")

    assert result is not None
    assert result.endswith("with_default_func.py")

  def test_returns_none_for_a_module_that_does_not_exist(self):
    assert _find_package_file(f"{_FIXTURES_PKG}.this_module_does_not_exist") is None


class TestResolveTargetExplicitFunction:
  def test_explicit_function_is_called_and_its_result_returned(self):
    result: Any = _resolve_target(f"{_FIXTURES_PKG}.with_named_func:custom_config_getter", "socket")

    assert result == {
      "local": {"source": "with_named_func"},
      "remote": {"source": "with_named_func"},
      "program_name": "named-func-program",
      "testing": False,
    }

  def test_missing_explicit_function_raises_attribute_error(self):
    with pytest.raises(AttributeError):
      _resolve_target(f"{_FIXTURES_PKG}.without_config_func:does_not_exist", "socket")


class TestResolveTargetDefaultFunction:
  def test_default_get_logging_configs_is_used_when_present(self):
    result: Any = _resolve_target(f"{_FIXTURES_PKG}.with_default_func", "socket")

    assert result == {
      "local": {"source": "with_default_func"},
      "remote": {"source": "with_default_func"},
      "program_name": "default-func-program",
      "testing": True,
    }

  def test_falls_back_to_base_logging_config_in_socket_mode(self):
    result: Any = _resolve_target(f"{_FIXTURES_PKG}.with_project_name_only", "socket")

    assert result["program_name"] == "constant-only-program"
    assert result["testing"] is True
    assert isinstance(result["local"], dict)
    assert isinstance(result["remote"], dict)
    assert result["remote"]  # a real remote config is built in socket mode

  def test_falls_back_with_an_empty_remote_config_in_main_mode(self):
    result: Any = _resolve_target(f"{_FIXTURES_PKG}.with_project_name_only", "main")

    assert result["remote"] == {}

  def test_a_module_that_fails_to_import_still_degrades_to_the_fallback(self):
    result: Any = _resolve_target(f"{_FIXTURES_PKG}.this_module_does_not_exist", "socket")

    assert set(result) == {"local", "remote", "program_name", "testing"}


class TestQueryLoggingConfigsEndToEnd:
  def test_default_function_result_is_returned_from_the_subprocess(self):
    result = query_logging_configs(f"{_FIXTURES_PKG}.with_default_func", mode="socket", timeout=_TIMEOUT)

    assert result == {
      "local": {"source": "with_default_func"},
      "remote": {"source": "with_default_func"},
      "program_name": "default-func-program",
      "testing": True,
    }

  def test_explicit_function_result_is_returned_from_the_subprocess(self):
    result = query_logging_configs(f"{_FIXTURES_PKG}.with_named_func:custom_config_getter", timeout=_TIMEOUT)

    assert result["program_name"] == "named-func-program"

  def test_missing_explicit_function_raises_runtime_error(self):
    with pytest.raises(RuntimeError):
      query_logging_configs(f"{_FIXTURES_PKG}.without_config_func:does_not_exist", timeout=_TIMEOUT)

  def test_a_hung_worker_raises_timeout_error(self):
    with pytest.raises(TimeoutError):
      query_logging_configs(f"{_FIXTURES_PKG}.hangs_forever", timeout=1.0)
