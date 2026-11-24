# Standard library imports
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

# First party imports
from aeth_ext.errors.shutdown import ShutdownKind
from aeth_ext.monitoring import health, heartbeat
from aeth_ext.settings import BaseSettings

assert not __debug__, "this harness must run under python -O"

# Recorded rather than run: the real shutdown would tear this process down mid-scenario.
shutdown_kinds: list[int] = []
heartbeat.run_shutdown = lambda kind=ShutdownKind.GRACEFUL: shutdown_kinds.append(kind)


def stalled_shutdown_check_stops_the_app() -> dict[str, object]:
  with tempfile.TemporaryDirectory() as tmp:
    BaseSettings.get_settings().log_loc_folder = Path(tmp)
    health.declare_health_check("db", period=60, grace=0, shutdown=True)
    health.send_health_check("db")
    # The tick judges an hour on, so this one heartbeat lands past the stall without waiting for it.
    real_tick = heartbeat.heartbeat_tick
    heartbeat.heartbeat_tick = lambda now: real_tick(now + timedelta(hours=1))
    heartbeat.send_heartbeat(None)
  return {"kinds": shutdown_kinds}


_SCENARIOS = {
  "stalled_shutdown_check_stops_the_app": stalled_shutdown_check_stops_the_app,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
