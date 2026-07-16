"""Tests for the `aeth_ext.logging.init` entry-point wiring.

`init_logging` / `init_logging_worker` / `init_logging_socket` introspect the
deepest `BaseLoggingConfig` subclass's configure method, gather its arguments
from uppercase ``__main__`` constants (via `parse_and_grab_constants`, stubbed
here), resolve the shared Rich console, apply defaults, and invoke it once.
"""

# Standard library imports
from typing import TYPE_CHECKING, Any, ClassVar

# Third party imports
import pytest
from rich import get_console

# First party imports
from aeth_ext.logging import init as init_mod
from aeth_ext.logging.setup import BaseLoggingConfig

if TYPE_CHECKING:
  # Third party imports
  from rich.console import Console


class _RecordingConfig:
  """Stands in for the deepest `BaseLoggingConfig` subclass."""

  main_calls: ClassVar[list[dict[str, Any]]] = []
  worker_calls: ClassVar[list[dict[str, Any]]] = []
  socket_calls: ClassVar[list[dict[str, Any]]] = []

  @classmethod
  def configure_logging_main(
    cls,
    rich_console: Console,
    project_name: str,
    asyncio: bool = False,
    logging_queues: tuple[Any, ...] | None = None,
  ) -> None:
    cls.main_calls.append(
      {"rich_console": rich_console, "project_name": project_name, "asyncio": asyncio, "logging_queues": logging_queues}
    )

  @classmethod
  def configure_logging_worker(cls, logging_queues: Any) -> None:
    cls.worker_calls.append({"logging_queues": logging_queues})

  @classmethod
  def configure_shared_socket_logging_client(cls, project_name: str) -> None:
    cls.socket_calls.append({"project_name": project_name})


@pytest.fixture(autouse=True)
def _fresh_init_state(monkeypatch: pytest.MonkeyPatch):
  """Reset the module's one-shot guard and pin the discovered config class."""
  monkeypatch.setattr(init_mod, "__initialized", False)
  monkeypatch.setattr(BaseLoggingConfig, "get_deepest_subclass", classmethod(lambda cls: _RecordingConfig))
  _RecordingConfig.main_calls.clear()
  _RecordingConfig.worker_calls.clear()
  _RecordingConfig.socket_calls.clear()


def _stub_constants(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> list[dict[str, str]]:
  """Replace `parse_and_grab_constants`, recording the constants requested."""
  requested: list[dict[str, str]] = []

  def fake_parse(expected_constants: dict[str, str], eval_locals: dict[str, Any]) -> dict[str, Any]:
    requested.append(dict(expected_constants))
    return values

  monkeypatch.setattr(init_mod, "parse_and_grab_constants", fake_parse)
  return requested


class TestInitLogging:
  def test_passes_parsed_constants_and_shared_console(self, monkeypatch: pytest.MonkeyPatch):
    requested = _stub_constants(monkeypatch, {"project_name": "test-proj"})

    init_mod.init_logging()

    (call,) = _RecordingConfig.main_calls
    assert call["project_name"] == "test-proj"
    assert call["rich_console"] is get_console()
    # init_logging's own arguments override constant lookup entirely.
    assert call["asyncio"] is False
    assert call["logging_queues"] == ()
    # Only the params not supplied by init_logging were searched for.
    (constants,) = requested
    assert constants == {"RICH_CONSOLE": "rich_console", "PROJECT_NAME": "project_name"}

  def test_missing_required_constant_raises(self, monkeypatch: pytest.MonkeyPatch):
    _stub_constants(monkeypatch, {})

    with pytest.raises(ValueError, match="Missing required arguments: project_name"):
      init_mod.init_logging()

    assert _RecordingConfig.main_calls == []

  def test_non_console_rich_console_constant_raises(self, monkeypatch: pytest.MonkeyPatch):
    _stub_constants(monkeypatch, {"project_name": "test-proj", "rich_console": "not a console"})

    with pytest.raises(TypeError, match="Expected 'rich_console' to be of type Console"):
      init_mod.init_logging()

  def test_second_initialization_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch):
    _stub_constants(monkeypatch, {"project_name": "test-proj"})

    init_mod.init_logging()
    init_mod.init_logging()
    init_mod.init_logging_worker(object())  # pyright: ignore[reportArgumentType]

    assert len(_RecordingConfig.main_calls) == 1
    assert _RecordingConfig.worker_calls == []


class TestInitLoggingWorker:
  def test_passes_queue_through(self, monkeypatch: pytest.MonkeyPatch):
    _stub_constants(monkeypatch, {})
    sentinel_queue = object()

    init_mod.init_logging_worker(sentinel_queue)  # pyright: ignore[reportArgumentType]

    (call,) = _RecordingConfig.worker_calls
    assert call["logging_queues"] is sentinel_queue


class TestInitLoggingSocket:
  def test_invokes_socket_configuration(self, monkeypatch: pytest.MonkeyPatch):
    _stub_constants(monkeypatch, {"project_name": "test-proj"})

    init_mod.init_logging_socket()

    (call,) = _RecordingConfig.socket_calls
    assert call["project_name"] == "test-proj"
