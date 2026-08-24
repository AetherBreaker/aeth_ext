"""Real, importable scenario functions for subprocess tests of `startup.main`.

`aeth_ext.errors.SHUTDOWN` is a one-shot, process-wide state that cannot be
reset once requested (see the root `tests/conftest.py`'s
`_clear_shutdown_state` guard), and `main`'s only shutdown path is requesting
it -- so exercising a real boot-then-shutdown cycle needs a fresh interpreter
per run, exactly like `_optimized_scenarios.py`. Written as genuine Python
source, not a code string, so IDE rename-symbol tooling can track these
references.
"""

# Standard library imports
import asyncio
import json
import sys
from pathlib import Path

# Third party imports
from pydantic import SecretStr

# `main`'s own boot-time "start" ping plus `run_heartbeat_async`'s initial
# (`send_start=False`) one -- the two the real-resolution scenario waits for
# before triggering shutdown. Mirrors `_EXPECTED_HEARTBEAT_PING_COUNT` in
# `test_startup.py`, which asserts on the same two.
_EXPECTED_REAL_PING_COUNT = 2


def _summarize_heartbeat_call(heartbeat_file: object, kwargs: dict[str, object]) -> dict[str, object]:
  """Reduce a send_heartbeat_async/run_heartbeat_async call to JSON-serializable fields."""
  tz = kwargs.get("tz")
  ping_url = kwargs.get("ping_url")
  pingkey = kwargs.get("pingkey")
  return {
    "heartbeat_file": str(heartbeat_file) if heartbeat_file is not None else None,
    "ping_url": ping_url.get_secret_value() if isinstance(ping_url, SecretStr) else ping_url,
    "pingkey": pingkey.get_secret_value() if isinstance(pingkey, SecretStr) else pingkey,
    "slug": kwargs.get("slug"),
    "start": kwargs.get("start"),
    "send_start": kwargs.get("send_start"),
    "tz": str(tz) if tz is not None else None,
  }


async def _boot_and_shut_down(log_dir: str) -> dict[str, object]:
  # Third party imports
  from aiologic import SimpleQueue

  # First party imports
  from aeth_ext.central_log_server import startup
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
  from aeth_ext.central_log_server.settings import Settings
  from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind

  log_path = Path(log_dir)
  ClientIdRegistry._path = log_path / "client_ids.json"  # pyright: ignore[reportPrivateUsage]
  settings = Settings.get_settings()
  settings.web_viewer_serve_host = "127.0.0.1"
  settings.web_viewer_serve_port = 0

  send_heartbeat_calls: list[dict[str, object]] = []
  run_heartbeat_async_calls: list[dict[str, object]] = []

  async def fake_send_heartbeat_async(heartbeat_file: object, **kwargs: object) -> None:
    send_heartbeat_calls.append(_summarize_heartbeat_call(heartbeat_file, kwargs))

  async def fake_run_heartbeat_async(heartbeat_file: object, **kwargs: object) -> None:
    run_heartbeat_async_calls.append(_summarize_heartbeat_call(heartbeat_file, kwargs))

  startup.send_heartbeat_async = fake_send_heartbeat_async
  startup.run_heartbeat_async = fake_run_heartbeat_async

  # main's boot sequence runs unconditionally before it ever consults
  # SHUTDOWN, so requesting it here is not a race: main still performs every
  # boot step for real, then finds it already requested the moment it reaches
  # `await SHUTDOWN` and proceeds straight into its shutdown path.
  SHUTDOWN.request(ShutdownKind.FATAL)

  log_queue = SimpleQueue()
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
  """Boot `main` with `send_heartbeat_async`/`run_heartbeat_async` left un-mocked,
  only stubbing the network call underneath them (`ping_healthcheck`), so the
  real `HEARTBEAT_SLUG` auto-detection actually runs end to end -- this is
  the exact path that silently stopped pinging in production."""
  # Third party imports
  from aiologic import SimpleQueue

  # First party imports
  from aeth_ext.central_log_server import startup
  from aeth_ext.central_log_server.server.id_registry import ClientIdRegistry
  from aeth_ext.central_log_server.settings import Settings
  from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind
  from aeth_ext.monitoring import heartbeat as heartbeat_module

  log_path = Path(log_dir)
  ClientIdRegistry._path = log_path / "client_ids.json"  # pyright: ignore[reportPrivateUsage]
  settings = Settings.get_settings()
  settings.web_viewer_serve_host = "127.0.0.1"
  settings.web_viewer_serve_port = 0
  settings.alerts_healthcheck_ping_url = None
  settings.alerts_healthcheck_pingkey = SecretStr("test-pingkey")

  ping_calls: list[dict[str, object]] = []

  # Unlike _boot_and_shut_down, SHUTDOWN is *not* requested here; this stub
  # triggers shutdown itself once both expected pings have landed.
  #
  # Both pings are dispatched with asyncio.to_thread (they block on a
  # synchronous HTTP request that must never run on the reader loop), so they
  # only arrive once the loop has actually driven them. Requesting shutdown up
  # front would cancel the periodic task before its own ping fired, and that
  # task's slug resolution is precisely what this scenario exists to exercise.
  #
  # Requesting SHUTDOWN from here is safe from the worker thread this runs on:
  # ShutdownState composes an aiologic.Event, which is thread/task safe by design.
  def fake_ping_healthcheck(url: SecretStr | None, **kwargs: object) -> None:
    ping_calls.append({"url": url.get_secret_value() if isinstance(url, SecretStr) else url, **kwargs})
    if len(ping_calls) >= _EXPECTED_REAL_PING_COUNT:
      SHUTDOWN.request(ShutdownKind.FATAL)

  heartbeat_module.ping_healthcheck = fake_ping_healthcheck

  log_queue = SimpleQueue()
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
