"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests
of `aeth_ext.errors.shutdown.install_shutdown_signal_handlers` (D-I3).

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` and this module is run in a
fresh `-O` interpreter by `_run_optimized` in `test_init.py`, since
`install_shutdown_signal_handlers` is a no-op under `__debug__ == True` (see
its docstring) and `__debug__` cannot be flipped at runtime.
"""

# Standard library imports
import json
import signal
import sys

# First party imports
from aeth_ext.errors import shutdown as shutdown_module

assert not __debug__, "this harness must run under python -O"


def _patch_signal_signal() -> tuple[list[int], list[object]]:
  """Replace `signal.signal` with a recorder instead of a real registration.

  This is a disposable, single-scenario subprocess, so mutating the real
  `signal` module directly (rather than needing a `monkeypatch` fixture) is
  safe -- nothing else in the process depends on real signal delivery.
  """
  registered_signums: list[int] = []
  registered_handlers: list[object] = []

  def fake_signal(sig: int, handler: object) -> None:
    registered_signums.append(sig)
    registered_handlers.append(handler)

  signal.signal = fake_signal
  return registered_signums, registered_handlers


def sigint_is_always_registered() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {"sigint_registered": signal.SIGINT in registered_signums}


def windows_registers_sigbreak_when_available_and_not_sigterm() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {
    "has_sigbreak": hasattr(signal, "SIGBREAK"),
    "sigbreak_registered": getattr(signal, "SIGBREAK", None) in registered_signums,
    "sigterm_registered": signal.SIGTERM in registered_signums,
  }


def posix_registers_sigterm() -> dict[str, object]:
  registered_signums, _ = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {"sigterm_registered": signal.SIGTERM in registered_signums}


def registers_the_module_level_handler() -> dict[str, object]:
  _, registered_handlers = _patch_signal_signal()
  shutdown_module.install_shutdown_signal_handlers()
  return {
    "handler_count": len(registered_handlers),
    "all_are_module_level_handler": all(h is shutdown_module._handle_shutdown_signal for h in registered_handlers),  # pyright: ignore[reportPrivateUsage]
  }


_SCENARIOS = {
  "sigint_is_always_registered": sigint_is_always_registered,
  "windows_registers_sigbreak_when_available_and_not_sigterm": windows_registers_sigbreak_when_available_and_not_sigterm,
  "posix_registers_sigterm": posix_registers_sigterm,
  "registers_the_module_level_handler": registers_the_module_level_handler,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
