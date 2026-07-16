"""Tests for `aeth_ext.logging.setup`."""

# Standard library imports
import io
import logging
import logging.handlers
import queue
import socket
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar, override

# Third party imports
import pytest
from rich.console import Console

# First party imports
import aeth_ext.central_log_server.client as client_mod
from aeth_ext.central_log_server.server.dispatch import QueueForwardHandler
from aeth_ext.logging import config as dc, setup as setup_mod
from aeth_ext.logging.bases import FixedRichHandler
from aeth_ext.logging.config import runtime_registry
from aeth_ext.logging.setup import BaseLoggingConfig

_PER_RUN_BACKUP_COUNT = 30
_EXPLICIT_HOST = "127.0.0.1"
_EXPLICIT_PORT = 12345
_SETTINGS_PORT = 4242


def _make_record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
  return logging.getLogRecordFactory()("test", level, __file__, 1, msg, None, None)


def _capture_console() -> Console:
  return Console(file=io.StringIO(), force_terminal=False, width=120)


class _CaptureHandler(logging.Handler):
  def __init__(self, level: int = logging.NOTSET) -> None:
    super().__init__(level)
    self.records: list[logging.LogRecord] = []

  @override
  def emit(self, record: logging.LogRecord) -> None:
    self.records.append(record)


@pytest.fixture(autouse=True)
def atexit_callbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
  """Redirect atexit registration and the log folder; restore global side effects."""
  old_factory = logging.getLogRecordFactory()
  old_hook = sys.excepthook
  callbacks: list = []
  monkeypatch.setattr(setup_mod, "atexit_register", callbacks.append)
  monkeypatch.setattr(setup_mod.settings, "log_loc_folder", tmp_path / "logs")
  monkeypatch.setattr(setup_mod.settings, "logging_config_loc", None)

  yield callbacks

  for cb in callbacks:
    with suppress(Exception):
      cb()
  logging.setLogRecordFactory(old_factory)
  sys.excepthook = old_hook


@pytest.fixture(autouse=True)
def _reset_preferred_formatter(monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setattr(setup_mod, "__preferred_file_formatter", None)


def _pin_deepest_subclass(monkeypatch: pytest.MonkeyPatch, cls: type[BaseLoggingConfig]) -> None:
  """Keep `configure_logging_extra` dispatch deterministic under the test runner."""
  monkeypatch.setattr(cls, "get_deepest_subclass", classmethod(lambda c: c))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestMakePerRunFileHandler:
  def test_returns_rotating_handler(self, tmp_path: Path):
    handler = setup_mod.make_per_run_file_handler(tmp_path / "run.txt")
    try:
      assert isinstance(handler, logging.handlers.RotatingFileHandler)
      assert handler.maxBytes == 0
      assert handler.backupCount == _PER_RUN_BACKUP_COUNT
    finally:
      handler.close()

  def test_writes_records_after_rollover(self, tmp_path: Path):
    log_file = tmp_path / "run.txt"
    handler = setup_mod.make_per_run_file_handler(log_file)
    try:
      handler.emit(_make_record("per-run message"))
      handler.flush()
      assert "per-run message" in log_file.read_text(encoding="utf-8")
    finally:
      handler.close()

  def test_rolls_over_existing_file(self, tmp_path: Path):
    log_file = tmp_path / "run.txt"
    log_file.write_text("previous run\n", encoding="utf-8")
    handler = setup_mod.make_per_run_file_handler(log_file)
    try:
      assert (tmp_path / "run.txt.1").read_text(encoding="utf-8") == "previous run\n"
    finally:
      handler.close()


class TestProbeSocketConnection:
  def test_returns_true_when_reachable(self):
    with socket.create_server(("127.0.0.1", 0)) as server:
      port = server.getsockname()[1]
      assert setup_mod._probe_socket_connection("127.0.0.1", port, "probe-test") is True  # pyright: ignore[reportPrivateUsage]

  def test_returns_false_when_refused(self):
    with socket.create_server(("127.0.0.1", 0)) as server:
      port = server.getsockname()[1]
    # Port has been released; nothing is listening on it anymore.
    assert setup_mod._probe_socket_connection("127.0.0.1", port, "probe-test") is False  # pyright: ignore[reportPrivateUsage]

  def test_returns_false_on_dns_failure(self):
    assert setup_mod._probe_socket_connection("host.invalid", 9999, "probe-test") is False  # pyright: ignore[reportPrivateUsage]


class TestEphemeralLogToConsole:
  def test_attaches_and_removes_handler(self):
    root = logging.getLogger()
    before = root.handlers[:]
    with setup_mod.ephemeral_log_to_console(_capture_console()) as handler:
      assert isinstance(handler, FixedRichHandler)
      assert handler in root.handlers
    assert root.handlers == before

  def test_restores_root_level(self):
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    with setup_mod.ephemeral_log_to_console(_capture_console()):
      assert root.level == logging.DEBUG
    assert root.level == logging.WARNING

  def test_removes_handler_on_exception(self):
    root = logging.getLogger()
    before = root.handlers[:]
    with pytest.raises(RuntimeError):
      with setup_mod.ephemeral_log_to_console(_capture_console()):
        raise RuntimeError("boom")
    assert root.handlers == before

  def test_messages_render_to_console(self):
    console = _capture_console()
    with setup_mod.ephemeral_log_to_console(console):
      logging.getLogger("ephemeral.test").info("visible message")
    assert "visible message" in console.file.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# BaseLoggingConfig internals
# ---------------------------------------------------------------------------


class TestRegisterHelpers:
  def test_register_format_values(self):
    class Cfg(BaseLoggingConfig):
      default_max_width = 33
      timestamp_format = "%H:%M"

    Cfg._register_format_values()  # pyright: ignore[reportPrivateUsage]
    assert "{libpath: <33}" in runtime_registry.resolve("log_format")
    assert runtime_registry.resolve("timestamp_format") == "%H:%M"

  def test_register_log_paths(self):
    BaseLoggingConfig._register_log_paths("myproj")  # pyright: ignore[reportPrivateUsage]
    folder = setup_mod.settings.log_loc_folder
    assert runtime_registry.resolve("debug_log_path") == folder / "myproj_debug.txt"
    assert runtime_registry.resolve("info_log_path") == folder / "myproj.txt"


class TestApplyConfig:
  def _register_worker_values(self):
    runtime_registry.register("worker_queue", queue.Queue())
    runtime_registry.register("root_level", "DEBUG")

  def test_applies_fragments(self):
    self._register_worker_values()
    BaseLoggingConfig._apply_config(["worker"])  # pyright: ignore[reportPrivateUsage]
    assert isinstance(logging.getHandlerByName("queue_out"), logging.handlers.QueueHandler)

  def test_modify_config_hook_runs(self):
    calls: list[dict[str, Any]] = []

    class Cfg(BaseLoggingConfig):
      @classmethod
      @override
      def modify_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        calls.append(config)
        config["root"]["level"] = "WARNING"
        return config

    self._register_worker_values()
    Cfg._apply_config(["worker"])  # pyright: ignore[reportPrivateUsage]
    assert calls and calls[0]["version"] == 1
    assert logging.getLogger().level == logging.WARNING

  def test_override_replace_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    (override_dir / "logging_config.toml").write_text(
      'version = 1\n\n[root]\nlevel = "ERROR"\n',
      encoding="utf-8",
    )
    monkeypatch.chdir(override_dir)
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(override_dir / "main.py"), raising=False)

    self._register_worker_values()
    BaseLoggingConfig._apply_config(["worker"])  # pyright: ignore[reportPrivateUsage]
    assert logging.getLogger().level == logging.ERROR
    assert logging.getHandlerByName("queue_out") is None

  def test_override_merge_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    (override_dir / "logging_config.toml").write_text(
      '[root]\n__merge__ = "deep"\nlevel = "ERROR"\n',
      encoding="utf-8",
    )
    monkeypatch.chdir(override_dir)
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(override_dir / "main.py"), raising=False)

    class Cfg(BaseLoggingConfig):
      override_mode = "merge"

    self._register_worker_values()
    Cfg._apply_config(["worker"])  # pyright: ignore[reportPrivateUsage]
    assert logging.getLogger().level == logging.ERROR
    assert isinstance(logging.getHandlerByName("queue_out"), logging.handlers.QueueHandler)


class TestStartQueueListeners:
  def test_starts_listeners_with_handlers_and_skips_empty(self, atexit_callbacks: list):
    dc.dict_config(
      {
        "version": 1,
        "handlers": {
          "sink": {"class": "logging.NullHandler"},
          "queued": {"class": "logging.handlers.QueueHandler", "handlers": ["sink"]},
          "outbound_only": {"class": "logging.handlers.QueueHandler"},
        },
        "root": {"level": "INFO", "handlers": ["queued", "outbound_only"]},
      }
    )
    BaseLoggingConfig._start_queue_listeners()  # pyright: ignore[reportPrivateUsage]

    queued = logging.getHandlerByName("queued")
    outbound = logging.getHandlerByName("outbound_only")
    assert queued.listener._thread is not None  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
    assert outbound.listener._thread is None  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
    assert atexit_callbacks == [queued.listener.stop]  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]

  def test_ignores_handlers_without_listeners(self, atexit_callbacks: list):
    dc.dict_config(
      {
        "version": 1,
        "handlers": {"plain": {"class": "logging.NullHandler"}},
        "root": {"level": "INFO", "handlers": ["plain"]},
      }
    )
    BaseLoggingConfig._start_queue_listeners()  # pyright: ignore[reportPrivateUsage]
    assert atexit_callbacks == []


class TestAttachQueueDrains:
  def test_drains_queue_into_root_handlers(self, atexit_callbacks: list):
    root = logging.getLogger()
    capture = _CaptureHandler()
    root.addHandler(capture)

    q: queue.Queue = queue.Queue()
    BaseLoggingConfig._attach_queue_drains(root, [q])  # pyright: ignore[reportPrivateUsage]
    assert len(atexit_callbacks) == 1

    q.put(_make_record("drained message"))
    deadline = time.monotonic() + 5.0
    while not capture.records and time.monotonic() < deadline:
      time.sleep(0.01)
    assert capture.records and capture.records[0].getMessage() == "drained message"

  def test_no_queues_is_noop(self, atexit_callbacks: list):
    BaseLoggingConfig._attach_queue_drains(logging.getLogger(), [])  # pyright: ignore[reportPrivateUsage]
    assert atexit_callbacks == []


# ---------------------------------------------------------------------------
# configure_logging_main
# ---------------------------------------------------------------------------


class TestConfigureLoggingMain:
  def test_daily_rich_sync(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj")

    assert Cfg.logging_file_name == "mainproj"
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    names = {h.name for h in root.handlers}
    assert names == {"debug_file", "info_file", "console"}
    assert isinstance(logging.getHandlerByName("console"), FixedRichHandler)
    assert setup_mod.settings.log_loc_folder.is_dir()

  def test_plain_console(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=True)

    console = logging.getHandlerByName("console")
    assert isinstance(console, logging.StreamHandler)
    assert not isinstance(console, FixedRichHandler)

  def test_no_console(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=False)

    assert {h.name for h in logging.getLogger().handlers} == {"debug_file", "info_file"}

  def test_per_run_logging_type(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      logging_type = "per_run"

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=False)

    debug_file = logging.getHandlerByName("debug_file")
    assert isinstance(debug_file, logging.handlers.RotatingFileHandler)
    assert not isinstance(debug_file, logging.handlers.TimedRotatingFileHandler)

  def test_async_queue_wraps_file_handlers(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", asyncio=True)

    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"queue_catchall", "console"}
    catchall = logging.getHandlerByName("queue_catchall")
    listener = catchall.listener  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
    assert {h.name for h in listener.handlers} == {"debug_file", "info_file"}
    assert listener._thread is not None  # started by _start_queue_listeners

  def test_async_queue_console_handler(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", asyncio=True, queue_console_handler=True)

    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"queue_catchall"}
    catchall = logging.getHandlerByName("queue_catchall")
    assert {h.name for h in catchall.listener.handlers} == {"debug_file", "info_file", "console"}  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]

  def test_extra_handlers_attached_to_root(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    extra = _CaptureHandler()
    Cfg.configure_logging_main(_capture_console(), "mainproj", extra_handlers=[extra])
    assert extra in logging.getLogger().handlers

  def test_logging_queues_are_drained(self, monkeypatch: pytest.MonkeyPatch, atexit_callbacks: list):
    class Cfg(BaseLoggingConfig):
      pass

    _pin_deepest_subclass(monkeypatch, Cfg)
    q: queue.Queue = queue.Queue()
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=False, logging_queues=[q])
    # One drain listener registered for the passed queue.
    assert len(atexit_callbacks) == 1

  def test_configure_logging_extra_receives_filtered_kwargs(self, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    class Cfg(BaseLoggingConfig):
      @classmethod
      @override
      def configure_logging_extra(cls, project_name: str, queue_console_handler: bool):
        captured["project_name"] = project_name
        captured["queue_console_handler"] = queue_console_handler

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj")
    assert captured == {"project_name": "mainproj", "queue_console_handler": False}

  def test_override_logdir_filename_resolves_locally(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override = tmp_path / "logging_config.toml"
    override.write_text(
      "[handlers.extra_file]\n"
      'class    = "logging.FileHandler"\n'
      'filename = "logdir://extra/extra.txt"\n'
      "delay    = true\n"
      'level    = "DEBUG"\n'
      "\n"
      "[root]\n"
      '__merge__ = "deep"\n'
      'handlers  = ["extra_file"]\n',
      encoding="utf-8",
    )
    monkeypatch.setattr(setup_mod.settings, "logging_config_loc", override)

    class Cfg(BaseLoggingConfig):
      override_mode = "merge"

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=False)

    handler = logging.getHandlerByName("extra_file")
    assert isinstance(handler, logging.FileHandler)
    expected = setup_mod.settings.log_loc_folder / "extra" / "extra.txt"
    assert Path(handler.baseFilename) == expected
    assert expected.parent.is_dir()

  def test_explicit_logging_file_name_is_kept(self, monkeypatch: pytest.MonkeyPatch):
    class Cfg(BaseLoggingConfig):
      logging_file_name = "custom_name"

    _pin_deepest_subclass(monkeypatch, Cfg)
    Cfg.configure_logging_main(_capture_console(), "mainproj", log_to_console=False)
    assert Cfg.logging_file_name == "custom_name"
    assert runtime_registry.resolve("debug_log_path").name == "custom_name_debug.txt"


# ---------------------------------------------------------------------------
# configure_logging_worker
# ---------------------------------------------------------------------------


class TestConfigureLoggingWorker:
  def test_raises_in_main_process_and_interpreter(self):
    with pytest.raises(RuntimeError, match="child processes or sub interpreters"):
      BaseLoggingConfig.configure_logging_worker(queue.Queue())

  def test_configures_forwarding_in_worker(self, monkeypatch: pytest.MonkeyPatch):
    # Standard library imports
    import multiprocessing

    class _FakeProcess:
      name = "SpawnPoolWorker-1"

    monkeypatch.setattr(multiprocessing, "current_process", _FakeProcess)

    q: queue.Queue = queue.Queue()
    BaseLoggingConfig.configure_logging_worker(q)

    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"queue_out"}
    queue_out = logging.getHandlerByName("queue_out")
    assert isinstance(queue_out, logging.handlers.QueueHandler)
    assert queue_out.queue is q
    # The worker's outbound listener must never be started locally.
    assert queue_out.listener._thread is None  # pyright: ignore[reportOptionalMemberAccess]

    logging.getLogger("worker.test").info("forwarded")
    assert q.get_nowait().getMessage() == "forwarded"


# ---------------------------------------------------------------------------
# _configure_logserver
# ---------------------------------------------------------------------------


class TestConfigureLogserver:
  def test_daily_logserver_config(self):
    # Third party imports
    from aiologic import Queue as AioQueue

    class Cfg(BaseLoggingConfig):
      pass

    q: AioQueue = AioQueue()
    server_config = Cfg._configure_logserver(q)  # pyright: ignore[reportPrivateUsage]

    # Global hierarchy: only the queue-forward handler, no file output.
    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"queue_forward"}
    assert isinstance(logging.getHandlerByName("queue_forward"), QueueForwardHandler)
    assert logging.getHandlerByName("debug_file") is None

    # The returned server-hierarchy config carries the file handlers instead.
    assert server_config["version"] == 1
    handlers = server_config["handlers"]
    assert set(handlers) == {"debug_file", "info_file"}
    assert handlers["debug_file"]["class"] == "aeth_ext.logging.bases.CustomTimedRotatingFileHandler"
    assert set(server_config["root"]["handlers"]) == {"debug_file", "info_file"}
    assert runtime_registry.resolve("debug_log_path").name == "log_server_debug.txt"

  def test_per_run_logserver_config(self):
    # Third party imports
    from aiologic import Queue as AioQueue

    class Cfg(BaseLoggingConfig):
      logging_type = "per_run"
      logging_file_name = "custom_server"

    server_config = Cfg._configure_logserver(AioQueue())  # pyright: ignore[reportPrivateUsage]

    handlers = server_config["handlers"]
    assert handlers["debug_file"]["()"] == "aeth_ext.logging.setup.make_per_run_file_handler"
    assert runtime_registry.resolve("debug_log_path").name == "custom_server_debug.txt"

  def test_returned_config_builds_a_working_hierarchy(self, tmp_path: Path):
    # Third party imports
    from aiologic import Queue as AioQueue

    # First party imports
    from aeth_ext.central_log_server.server.dispatch import build_hierarchy, shutdown_hierarchy

    class Cfg(BaseLoggingConfig):
      pass

    server_config = Cfg._configure_logserver(AioQueue())  # pyright: ignore[reportPrivateUsage]
    manager, root = build_hierarchy(server_config, tmp_path)
    try:
      assert {h.get_name() for h in root.handlers} == {"debug_file", "info_file"}
      # The private hierarchy must not touch the global handler registry.
      assert logging.getHandlerByName("debug_file") is None
    finally:
      shutdown_hierarchy(manager, root)


# ---------------------------------------------------------------------------
# Socket client configuration
# ---------------------------------------------------------------------------


class _StubSocketHandler(logging.Handler):
  last_kwargs: ClassVar[dict[str, Any]] = {}
  verify_calls: ClassVar[int] = 0

  def __init__(self, **kwargs: Any) -> None:
    type(self).last_kwargs = kwargs
    super().__init__()

  def connect_and_verify(self) -> None:
    type(self).verify_calls += 1


@pytest.fixture
def stub_socket_handler(monkeypatch: pytest.MonkeyPatch):
  _StubSocketHandler.last_kwargs = {}
  _StubSocketHandler.verify_calls = 0
  monkeypatch.setattr(client_mod, "HandshakeSocketHandler", _StubSocketHandler)
  monkeypatch.setattr(setup_mod, "_probe_socket_connection", lambda host, port, project_name: True)
  return _StubSocketHandler


class TestGetDefaultRemoteConfig:
  def test_daily_config(self):
    config = BaseLoggingConfig.get_default_remote_config("proj")
    assert config["version"] == 1
    handlers = config["handlers"]
    assert handlers["debug_file"]["class"] == "aeth_ext.logging.bases.CustomTimedRotatingFileHandler"
    assert handlers["debug_file"]["when"] == "midnight"
    # Filenames stay as server-side logdir:// references...
    assert handlers["debug_file"]["filename"] == "logdir://proj_debug.txt"
    assert handlers["info_file"]["filename"] == "logdir://proj.txt"
    # ...while formatter values were resolved client-side by pre_resolve.
    fmt = config["formatters"]["preferred"]["fmt"]
    assert "runtime://" not in fmt and "{libpath" in fmt
    assert set(config["root"]["handlers"]) == {"debug_file", "info_file"}

  def test_per_run_config(self):
    class Cfg(BaseLoggingConfig):
      logging_type = "per_run"

    config = Cfg.get_default_remote_config("proj")
    handlers = config["handlers"]
    assert handlers["debug_file"]["()"] == "aeth_ext.logging.setup.make_per_run_file_handler"
    assert handlers["info_file"]["backupCount"] == _PER_RUN_BACKUP_COUNT

  def test_remote_override_file_merges_onto_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / setup_mod.REMOTE_OVERRIDE_FILENAME).write_text(
      "[handlers.scheduler_file]\n"
      'class     = "aeth_ext.logging.bases.CustomTimedRotatingFileHandler"\n'
      'filename  = "logdir://scheduler.txt"\n'
      'formatter = "preferred"\n'
      "\n"
      "[loggers.apscheduler]\n"
      'level     = "DEBUG"\n'
      'handlers  = ["scheduler_file"]\n'
      "propagate = false\n",
      encoding="utf-8",
    )
    monkeypatch.setattr(setup_mod.settings, "logging_config_loc", cfg_dir)

    config = BaseLoggingConfig.get_default_remote_config("proj")

    # Override entries were merged in...
    assert config["handlers"]["scheduler_file"]["filename"] == "logdir://scheduler.txt"
    assert config["loggers"]["apscheduler"] == {"level": "DEBUG", "handlers": ["scheduler_file"], "propagate": False}
    # ...while the packaged defaults were retained.
    assert config["handlers"]["debug_file"]["filename"] == "logdir://proj_debug.txt"
    assert set(config["root"]["handlers"]) == {"debug_file", "info_file"}


class TestConfigureSharedSocketLoggingClient:
  def test_probe_failure_raises(self, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setup_mod, "_probe_socket_connection", lambda host, port, project_name: False)

    class Cfg(BaseLoggingConfig):
      pass

    with pytest.raises(RuntimeError, match="Failed to connect to log server"):
      Cfg.configure_shared_socket_logging_client("proj", _capture_console(), host=_EXPLICIT_HOST, port=59999)

  def test_socket_only_configuration(self, stub_socket_handler: type[_StubSocketHandler]):
    class Cfg(BaseLoggingConfig):
      pass

    Cfg.configure_shared_socket_logging_client("proj", _capture_console(), host=_EXPLICIT_HOST, port=_EXPLICIT_PORT)

    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"socket"}
    assert isinstance(logging.getHandlerByName("socket"), stub_socket_handler)

    kwargs = stub_socket_handler.last_kwargs
    assert kwargs["program_name"] == "proj"
    assert kwargs["host"] == _EXPLICIT_HOST
    assert kwargs["port"] == _EXPLICIT_PORT
    remote = kwargs["config"]
    assert set(remote["handlers"]) == {"debug_file", "info_file"}
    # A rejected handshake must fail fast, so the handler was verified eagerly.
    assert stub_socket_handler.verify_calls == 1

  def test_socket_mode_uses_socket_override_file(
    self, stub_socket_handler: type[_StubSocketHandler], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
  ):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    # The main-mode override must NOT apply in socket mode...
    (cfg_dir / "logging_config.toml").write_text('[loggers.main_marker]\nlevel = "CRITICAL"\n', encoding="utf-8")
    # ...while the socket-mode override must.
    (cfg_dir / setup_mod.SOCKET_OVERRIDE_FILENAME).write_text('[loggers.socket_marker]\nlevel = "WARNING"\n', encoding="utf-8")
    monkeypatch.setattr(setup_mod.settings, "logging_config_loc", cfg_dir)

    class Cfg(BaseLoggingConfig):
      override_mode = "merge"

    Cfg.configure_shared_socket_logging_client("proj", _capture_console(), host=_EXPLICIT_HOST, port=_EXPLICIT_PORT)

    assert logging.getLogger("socket_marker").level == logging.WARNING
    assert logging.getLogger("main_marker").level == logging.NOTSET

  def test_host_port_default_from_settings(self, stub_socket_handler: type[_StubSocketHandler], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(setup_mod.settings, "log_conn_host", "settings-host")
    monkeypatch.setattr(setup_mod.settings, "log_conn_port", _SETTINGS_PORT)

    class Cfg(BaseLoggingConfig):
      pass

    Cfg.configure_shared_socket_logging_client("proj", _capture_console())
    assert stub_socket_handler.last_kwargs["host"] == "settings-host"
    assert stub_socket_handler.last_kwargs["port"] == _SETTINGS_PORT

  def test_explicit_remote_config_used(self, stub_socket_handler: type[_StubSocketHandler]):
    class Cfg(BaseLoggingConfig):
      pass

    custom_config = {
      "version": 1,
      "handlers": {"only": {"class": "logging.NullHandler"}},
      "root": {"level": "INFO", "handlers": ["only"]},
    }
    Cfg.configure_shared_socket_logging_client(
      "proj", _capture_console(), host=_EXPLICIT_HOST, port=_EXPLICIT_PORT, remote_config=custom_config
    )
    assert stub_socket_handler.last_kwargs["config"] == custom_config

  def test_testing_mode_adds_local_logging(self, stub_socket_handler: type[_StubSocketHandler]):
    class Cfg(BaseLoggingConfig):
      pass

    Cfg.configure_shared_socket_logging_client("proj", _capture_console(), host=_EXPLICIT_HOST, port=_EXPLICIT_PORT, testing=True)

    root = logging.getLogger()
    assert {h.name for h in root.handlers} == {"queue_catchall", "console", "socket"}
    catchall = logging.getHandlerByName("queue_catchall")
    assert {h.name for h in catchall.listener.handlers} == {"debug_file", "info_file"}  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
    assert catchall.listener._thread is not None  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
