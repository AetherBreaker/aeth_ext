"""Tests for `aeth_ext.__init__` (the `initialize()` entrypoint helper).

`_handle_shutdown_signal` is exercised only through its effect on
`signal.signal` registration, never actually invoked: it sets the
process-global, one-shot `SHUTDOWN_EVENT` unconditionally (no `__debug__`
guard, unlike the `err_handling` alert helpers), so calling it for real would
permanently poison the rest of this pytest session.
"""

# Standard library imports
import signal
from sys import platform

# Third party imports
import pytest

# First party imports
import aeth_ext


class TestRegisterShutdownSignalHandlers:
  """D-G2/D-G3: platform-appropriate signal registration."""

  def test_sigint_is_always_registered(self, monkeypatch: pytest.MonkeyPatch):
    registered: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.append(sig))

    aeth_ext._register_shutdown_signal_handlers()  # pyright: ignore[reportPrivateUsage]

    assert signal.SIGINT in registered

  @pytest.mark.skipif(platform != "win32", reason="Windows-specific signal set")
  def test_windows_registers_sigbreak_when_available_and_not_sigterm(self, monkeypatch: pytest.MonkeyPatch):
    registered: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.append(sig))

    aeth_ext._register_shutdown_signal_handlers()  # pyright: ignore[reportPrivateUsage]

    if hasattr(signal, "SIGBREAK"):
      assert signal.SIGBREAK in registered
    assert signal.SIGTERM not in registered

  @pytest.mark.skipif(platform == "win32", reason="POSIX-specific signal set")
  def test_posix_registers_sigterm(self, monkeypatch: pytest.MonkeyPatch):
    registered: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.append(sig))

    aeth_ext._register_shutdown_signal_handlers()  # pyright: ignore[reportPrivateUsage]

    assert signal.SIGTERM in registered


class TestHandleShutdownSignalSetsTheModuleLevelHandler:
  """Confirms the registered callback is `_handle_shutdown_signal` itself, without invoking it."""

  def test_registers_the_module_level_handler(self, monkeypatch: pytest.MonkeyPatch):
    registered_handlers: list[object] = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: registered_handlers.append(handler))

    aeth_ext._register_shutdown_signal_handlers()  # pyright: ignore[reportPrivateUsage]

    assert all(handler is aeth_ext._handle_shutdown_signal for handler in registered_handlers)  # pyright: ignore[reportPrivateUsage]


class TestInitializeInstallSignalHandlersFlag:
  def test_default_registers_signal_handlers(self, monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(aeth_ext, "_register_shutdown_signal_handlers", lambda: calls.append(True))

    aeth_ext.initialize(logging=False, run_monkey_patches=False)

    assert calls == [True]

  def test_opted_out_skips_registration(self, monkeypatch: pytest.MonkeyPatch):
    calls: list[bool] = []
    monkeypatch.setattr(aeth_ext, "_register_shutdown_signal_handlers", lambda: calls.append(True))

    aeth_ext.initialize(logging=False, run_monkey_patches=False, install_signal_handlers=False)

    assert calls == []
