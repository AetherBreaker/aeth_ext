"""Real, importable scenario functions for `-O` (`__debug__ == False`) subprocess tests.

Written as genuine Python source -- not string-embedded code passed to
`python -c` -- so IDE rename-symbol tooling can track and update references
to the symbols under test here exactly like any other file in the suite.
Each scenario is selected by name via `argv[1]` and this module is run in a
fresh `-O` interpreter by `_run_optimized` in `test_startup.py`, since
`HEARTBEAT_FILE` is only a real path (rather than `None`) under
`__debug__ == False`.
"""

# Standard library imports
import json
import sys

# First party imports
from aeth_ext.central_log_server import startup

assert not __debug__, "this harness must run under python -O"


def heartbeat_file_is_a_real_path_outside_debug_mode() -> dict[str, object]:
  return {
    "heartbeat_file_is_none": startup.HEARTBEAT_FILE is None,
    "heartbeat_file_name": startup.HEARTBEAT_FILE.name if startup.HEARTBEAT_FILE is not None else None,
  }


_SCENARIOS = {
  "heartbeat_file_is_a_real_path_outside_debug_mode": heartbeat_file_is_a_real_path_outside_debug_mode,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
