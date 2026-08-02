"""Real, importable scenario functions for subprocess tests of `startup.main`.

`aeth_ext.errors.FATAL_EVENT` is a one-shot `aiologic.Event` that cannot be
reset once set (see the root `tests/conftest.py`'s `_clear_fatal_event`
guard), and `main`'s only shutdown path is triggering that event -- so
exercising a real boot-then-shutdown cycle needs a fresh interpreter per run,
exactly like `_optimized_scenarios.py`. Written as genuine Python source, not
a code string, so IDE rename-symbol tooling can track these references.
"""

# Standard library imports
import asyncio
import json
import sys
from pathlib import Path


def _summarize_heartbeat_call(heartbeat_file: object, kwargs: dict[str, object]) -> dict[str, object]:
  """Reduce a send_heartbeat/run_heartbeat_async call to JSON-serializable fields."""
  tz = kwargs.get("tz")
  return {
    "heartbeat_file": str(heartbeat_file) if heartbeat_file is not None else None,
    "ping_url": kwargs.get("ping_url"),
    "pingkey": kwargs.get("pingkey"),
    "slug": kwargs.get("slug"),
    "start": kwargs.get("start"),
    "send_start": kwargs.get("send_start"),
    "tz": str(tz) if tz is not None else None,
  }


async def _boot_and_shut_down(log_dir: str) -> dict[str, object]:
  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.central_log_server import startup
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
  from aeth_ext.central_log_server.settings import Settings
  from aeth_ext.errors import FATAL_EVENT

  log_path = Path(log_dir)
  ClientIdRegistry._path = log_path / "client_ids.json"  # pyright: ignore[reportPrivateUsage]
  settings = Settings.get_settings()
  settings.web_viewer_serve_host = "127.0.0.1"
  settings.web_viewer_serve_port = 0

  send_heartbeat_calls: list[dict[str, object]] = []
  run_heartbeat_async_calls: list[dict[str, object]] = []

  def fake_send_heartbeat(heartbeat_file: object, **kwargs: object) -> None:
    send_heartbeat_calls.append(_summarize_heartbeat_call(heartbeat_file, kwargs))

  async def fake_run_heartbeat_async(heartbeat_file: object, **kwargs: object) -> None:
    run_heartbeat_async_calls.append(_summarize_heartbeat_call(heartbeat_file, kwargs))

  startup.send_heartbeat = fake_send_heartbeat
  startup.run_heartbeat_async = fake_run_heartbeat_async

  # main's boot sequence runs unconditionally before it ever consults
  # FATAL_EVENT, so pre-setting it here is not a race: main still performs
  # every boot step for real, then finds the event already set the moment it
  # reaches `await FATAL_EVENT` and proceeds straight into its shutdown path.
  FATAL_EVENT.set()

  log_queue = Queue()
  await asyncio.wait_for(
    startup.main(log_queue=log_queue, host="127.0.0.1", port=0, log_dir=log_path, server_config=None),
    timeout=10,
  )
  return {
    "completed": True,
    "send_heartbeat_calls": send_heartbeat_calls,
    "run_heartbeat_async_calls": run_heartbeat_async_calls,
  }


def main_boots_and_shuts_down_cleanly(log_dir: str) -> dict[str, object]:
  return asyncio.run(_boot_and_shut_down(log_dir))


async def _boot_with_real_heartbeat_resolution(log_dir: str) -> dict[str, object]:
  """Boot `main` with `send_heartbeat`/`run_heartbeat_async` left un-mocked,
  only stubbing the network call underneath them (`ping_healthcheck`), so the
  real `HEARTBEAT_SLUG` auto-detection actually runs end to end -- this is
  the exact path that silently stopped pinging in production."""
  # Third party imports
  from aiologic import Queue

  # First party imports
  from aeth_ext.central_log_server import startup
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
  from aeth_ext.central_log_server.settings import Settings
  from aeth_ext.errors import FATAL_EVENT
  from aeth_ext.monitoring import heartbeat as heartbeat_module

  log_path = Path(log_dir)
  ClientIdRegistry._path = log_path / "client_ids.json"  # pyright: ignore[reportPrivateUsage]
  settings = Settings.get_settings()
  settings.web_viewer_serve_host = "127.0.0.1"
  settings.web_viewer_serve_port = 0
  settings.alerts_healthcheck_ping_url = None
  settings.alerts_healthcheck_pingkey = "test-pingkey"

  ping_calls: list[dict[str, object]] = []

  def fake_ping_healthcheck(url: str | None, **kwargs: object) -> None:
    ping_calls.append({"url": url, **kwargs})

  heartbeat_module.ping_healthcheck = fake_ping_healthcheck

  FATAL_EVENT.set()

  log_queue = Queue()
  await asyncio.wait_for(
    startup.main(log_queue=log_queue, host="127.0.0.1", port=0, log_dir=log_path, server_config=None),
    timeout=10,
  )
  return {"completed": True, "ping_calls": ping_calls}


def main_boots_with_real_heartbeat_resolution(log_dir: str) -> dict[str, object]:
  return asyncio.run(_boot_with_real_heartbeat_resolution(log_dir))


_SCENARIOS = {
  "main_boots_and_shuts_down_cleanly": main_boots_and_shuts_down_cleanly,
  "main_boots_with_real_heartbeat_resolution": main_boots_with_real_heartbeat_resolution,
}


if __name__ == "__main__":
  scenario_name = sys.argv[1]
  scenario_args = sys.argv[2:]
  result = _SCENARIOS[scenario_name](*scenario_args)
  print(json.dumps(result))
