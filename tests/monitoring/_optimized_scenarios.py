# Standard library imports
import json
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors.shutdown import ShutdownKind
from aeth_ext.monitoring import health, heartbeat
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Third party imports
  from pydantic import SecretStr

assert not __debug__, "this harness must run under python -O"

# Recorded rather than run: the real shutdown would tear this process down mid-scenario.
shutdown_kinds: list[int] = []
heartbeat.run_shutdown = lambda kind=ShutdownKind.GRACEFUL: shutdown_kinds.append(kind)


def stalled_shutdown_check_stops_the_app() -> dict[str, object]:
  failures: list[bool] = []
  pinged = threading.Event()

  # A resolver wedged past `urlopen`'s timeout: no ping returns, so a regular ping on the heartbeat's own
  # thread would hang the scenario, and the alert's wait has to run out.
  def wedged_ping(
    url: SecretStr | None, *, failure: bool = False, start: bool = False, autoprovision: bool = False, body: str | None = None
  ) -> None:
    del url, start, autoprovision, body
    failures.append(failure)
    pinged.set()
    threading.Event().wait()

  heartbeat.ping_healthcheck = wedged_ping
  # Low, so the scenario stays fast.
  heartbeat._SHUTDOWN_PING_WAIT_SECS = 0.1  # pyright: ignore[reportPrivateUsage]
  with tempfile.TemporaryDirectory() as tmp:
    BaseSettings.get_settings().log_loc_folder = Path(tmp)
    health.declare_health_check("db", period=60, grace=0, fail_ping=True, shutdown=True)
    health.send_health_check("db")
    # The tick judges an hour on, so this one heartbeat lands past the stall without waiting for it.
    real_tick = heartbeat.heartbeat_tick
    heartbeat.heartbeat_tick = lambda now: real_tick(now + timedelta(hours=1))
    heartbeat.send_heartbeat(None)
  # Only in case a loaded machine scheduled the alert's thread after the wait ran out.
  pinged.wait(timeout=5)
  return {"kinds": shutdown_kinds, "failures": failures}


_SCENARIOS = {
  "stalled_shutdown_check_stops_the_app": stalled_shutdown_check_stops_the_app,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  result = _SCENARIOS[scenario_name]()
  print(json.dumps(result))
