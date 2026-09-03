# Standard library imports
from logging.handlers import DEFAULT_TCP_LOGGING_PORT
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest
from aiologic import SimpleQueue

# First party imports
from aeth_ext.central_log_server import __main__ as main_module
from aeth_ext.logging.setup import BaseLoggingConfig

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

_CUSTOM_PORT = 12345


@pytest.fixture(autouse=True)
def _stub_collaborators(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  init_calls: list[dict[str, Any]] = []
  monkeypatch.setattr(main_module, "initialize", lambda **kw: init_calls.append(kw))
  monkeypatch.setattr(BaseLoggingConfig, "_configure_logserver", staticmethod(lambda _queue: {"fake": "server_config"}))

  main_calls: list[dict[str, Any]] = []

  async def fake_main(**kwargs: Any) -> None:
    main_calls.append(kwargs)

  monkeypatch.setattr(main_module, "main", fake_main)
  return init_calls, main_calls


class TestCli:
  def test_defaults_omit_log_dir_and_wire_expected_kwargs(self, _stub_collaborators: tuple[list[Any], list[Any]]) -> None:
    init_calls, main_calls = _stub_collaborators

    main_module.cli()

    assert init_calls == [{"asyncio": True, "logging": False}]
    (kwargs,) = main_calls
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == DEFAULT_TCP_LOGGING_PORT
    assert kwargs["server_config"] == {"fake": "server_config"}
    assert isinstance(kwargs["log_queue"], SimpleQueue)
    assert "log_dir" not in kwargs

  def test_explicit_log_dir_is_forwarded(self, _stub_collaborators: tuple[list[Any], list[Any]], tmp_path: Path) -> None:
    _init_calls, main_calls = _stub_collaborators

    main_module.cli(host="127.0.0.1", port=_CUSTOM_PORT, log_dir=tmp_path)

    (kwargs,) = main_calls
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == _CUSTOM_PORT
    assert kwargs["log_dir"] == tmp_path
