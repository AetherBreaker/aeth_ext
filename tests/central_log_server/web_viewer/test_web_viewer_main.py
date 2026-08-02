"""Tests for the `python -m aeth_ext.central_log_server.web_viewer` script entrypoint.

The module's real logic lives directly under its own `if __name__ == "__main__":`
block rather than in a callable function, so `runpy.run_module` (with
`run_name="__main__"`) is used to actually execute that block -- the same
mechanism Python itself uses for `python -m`. `initialize` and
`LogWebViewApp.run` are patched via their real module/class references before
each run so no real logging socket or Textual app is ever touched.
"""

# Standard library imports
import runpy
from typing import Any

# Third party imports
import pytest

# First party imports
import aeth_ext
from aeth_ext.central_log_server.web_viewer import LogWebViewApp

_MODULE_NAME = "aeth_ext.central_log_server.web_viewer.__main__"


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[None]:
  calls: list[None] = []
  monkeypatch.setattr(LogWebViewApp, "run", lambda self: calls.append(None))
  return calls


def _run_entrypoint() -> None:
  runpy.run_module(_MODULE_NAME, run_name="__main__")


class TestSocketLoggingAvailable:
  def test_initializes_socket_logging_and_runs_the_app(self, monkeypatch: pytest.MonkeyPatch, run_calls: list[None]):
    init_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(aeth_ext, "initialize", lambda **kw: init_calls.append(kw))

    _run_entrypoint()

    assert init_calls == [{"logging": "socket"}]
    assert run_calls == [None]


class TestSocketLoggingUnavailable:
  def test_falls_back_to_local_logging_when_log_server_unreachable(self, monkeypatch: pytest.MonkeyPatch, run_calls: list[None]):
    init_calls: list[dict[str, Any]] = []

    def fake_initialize(**kwargs: Any) -> None:
      init_calls.append(kwargs)
      if kwargs == {"logging": "socket"}:
        raise RuntimeError("Failed to connect to log server at 127.0.0.1:9020")

    monkeypatch.setattr(aeth_ext, "initialize", fake_initialize)

    _run_entrypoint()

    assert init_calls == [{"logging": "socket"}, {"logging": True}]
    assert run_calls == [None]

  def test_reraises_unrelated_runtime_errors_without_falling_back(self, monkeypatch: pytest.MonkeyPatch, run_calls: list[None]):
    def fake_initialize(**_kwargs: Any) -> None:
      raise RuntimeError("something else entirely broke")

    monkeypatch.setattr(aeth_ext, "initialize", fake_initialize)

    with pytest.raises(RuntimeError, match="something else entirely broke"):
      _run_entrypoint()

    assert run_calls == []
