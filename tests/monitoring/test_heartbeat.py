# Standard library imports
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

# Third party imports
import aiologic
import pytest
from pydantic import SecretStr

# First party imports
from aeth_ext.monitoring import health, heartbeat as heartbeat_module
from aeth_ext.settings import BaseSettings

# Local imports
from tests.conftest import wait_until

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Coroutine
  from pathlib import Path
  from typing import Any

# 1 start ping + at least 2 subsequent plain pings expected in the "sends on
# the configured interval" tests below.
_MIN_EXPECTED_PING_CALLS = 3

# Iterations of the "the loop is still scheduling other work" ticker used by the
# tests that hold a heartbeat's blocking ping open, and the wall-clock budget
# they must finish within. The ticker nominally takes ~50ms; the ping it runs
# against is held for _HELD_PING_SECONDS. Asserting on elapsed time rather than
# on the tick count is what makes those tests meaningful -- a stalled loop still
# reaches the full count eventually, just _HELD_PING_SECONDS late.
_EXPECTED_LOOP_TICKS = 5
_HELD_PING_SECONDS = 5.0
_MAX_TICKER_SECONDS = 1.0

# ceil(90.5), and the period the built-in check was declared with.
_CUSTOM_INTERVAL_PERIOD = 91
_BUILTIN_PERIOD = 60


class TestSendHeartbeat:
  def test_writes_current_timestamp_to_the_heartbeat_file(self, tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"

    heartbeat_module.send_heartbeat(heartbeat_file)

    written = heartbeat_file.read_text()
    assert datetime.fromisoformat(written)

  def test_none_heartbeat_file_skips_the_local_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
    ping_calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: ping_calls.append((a, k)))

    heartbeat_module.send_heartbeat(None)  # must not raise

  def test_write_failure_is_logged_not_raised(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing_dir_file = tmp_path / "does_not_exist" / "heartbeat.txt"

    with caplog.at_level("ERROR"):
      heartbeat_module.send_heartbeat(missing_dir_file)  # must not raise

    assert any("Failed to write heartbeat file" in record.message for record in caplog.records)

  def test_ping_url_takes_precedence_over_pingkey_and_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(
      tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/fixed-uuid"), pingkey=SecretStr("key"), slug="my-app"
    )

    assert calls == [(SecretStr("https://hc-ping.com/fixed-uuid"), False, False, False)]

  def test_pingkey_and_slug_build_the_autoprovisioning_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey=SecretStr("my-ping-key"), slug="my-app")

    assert calls == [(SecretStr("https://hc-ping.com/my-ping-key/my-app"), False, False, True)]

  def test_no_ping_configuration_results_in_a_none_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt")

    assert calls == [(None, False, False, False)]

  def test_uses_the_settings_slug_when_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(BaseSettings.get_settings(), "heartbeat_slug", "settings-slug")

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey=SecretStr("my-ping-key"))

    assert calls == [(SecretStr("https://hc-ping.com/my-ping-key/settings-slug"), False, False, True)]

  def test_explicit_slug_overrides_the_setting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SecretStr | None] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda url, **_: calls.append(url))
    monkeypatch.setattr(BaseSettings.get_settings(), "heartbeat_slug", "settings-slug")

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey=SecretStr("my-ping-key"), slug="explicit-slug")

    assert calls == [SecretStr("https://hc-ping.com/my-ping-key/explicit-slug")]

  def test_start_and_failure_flags_are_forwarded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), start=True)
    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), failure=True)

    assert calls == [
      (SecretStr("https://hc-ping.com/uuid"), False, True, False),
      (SecretStr("https://hc-ping.com/uuid"), True, False, False),
    ]


class TestSendHeartbeatAsync:
  async def test_runs_the_blocking_work_off_the_event_loop_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ping_threads: list[int] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda _url, **_kwargs: ping_threads.append(threading.get_ident()),
    )

    await heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"))

    assert ping_threads
    assert threading.get_ident() not in ping_threads

  async def test_a_blocking_ping_does_not_stall_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: release.wait(timeout=_HELD_PING_SECONDS))

    heartbeat = asyncio.ensure_future(
      heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"))
    )
    # The loop must keep scheduling other work while that ping is blocked, and
    # must do so *promptly* -- a stalled loop still completes every tick, just
    # once the held ping finally releases.
    started = time.perf_counter()
    ticks = 0
    for _ in range(_EXPECTED_LOOP_TICKS):
      await asyncio.sleep(0.01)
      ticks += 1
    elapsed = time.perf_counter() - started

    assert ticks == _EXPECTED_LOOP_TICKS
    assert elapsed < _MAX_TICKER_SECONDS, f"event loop was stalled by the blocking ping ({elapsed:.2f}s)"
    assert not heartbeat.done()

    release.set()
    await heartbeat

  async def test_uses_the_settings_slug_when_omitted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(BaseSettings.get_settings(), "heartbeat_slug", "settings-slug")

    await heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", pingkey=SecretStr("my-ping-key"))

    assert calls == [(SecretStr("https://hc-ping.com/my-ping-key/settings-slug"), False, False, True)]

  async def test_writes_the_heartbeat_file_and_forwards_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    heartbeat_file = tmp_path / "heartbeat.txt"

    await heartbeat_module.send_heartbeat_async(heartbeat_file, ping_url=SecretStr("https://hc-ping.com/uuid"), start=True)

    assert calls == [(SecretStr("https://hc-ping.com/uuid"), False, True, False)]
    assert datetime.fromisoformat(heartbeat_file.read_text())


async def _run_briefly(coro: Coroutine[Any, Any, object], seconds: float) -> None:
  task = asyncio.ensure_future(coro)
  await asyncio.sleep(seconds)
  task.cancel()
  try:
    await task
  except asyncio.CancelledError:
    pass


class TestRunHeartbeatAsync:
  async def test_sends_a_start_ping_immediately(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10),
      0.01,
    )

    assert calls == [(SecretStr("https://hc-ping.com/uuid"), False, True, False)]

  async def test_runs_the_blocking_work_off_the_event_loop_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ping_threads: list[int] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda _url, **_kwargs: ping_threads.append(threading.get_ident()),
    )
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10),
      0.05,
    )

    assert ping_threads
    assert threading.get_ident() not in ping_threads

  async def test_a_blocking_ping_does_not_stall_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: release.wait(timeout=_HELD_PING_SECONDS))
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())

    task = asyncio.ensure_future(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=0.01)
    )
    started = time.perf_counter()
    ticks = 0
    for _ in range(_EXPECTED_LOOP_TICKS):
      await asyncio.sleep(0.01)
      ticks += 1
    elapsed = time.perf_counter() - started

    assert ticks == _EXPECTED_LOOP_TICKS
    assert elapsed < _MAX_TICKER_SECONDS, f"event loop was stalled by the blocking ping ({elapsed:.2f}s)"

    release.set()
    task.cancel()
    try:
      await task
    except asyncio.CancelledError:
      pass

  async def test_send_start_false_sends_a_plain_initial_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(
        tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10, send_start=False
      ),
      0.01,
    )

    assert calls == [(SecretStr("https://hc-ping.com/uuid"), False, False, False)]

  async def test_sends_subsequent_plain_pings_on_the_configured_interval(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())

    # Polled rather than a fixed sleep window: a fixed ~90ms budget at a 20ms interval assumes the
    # scheduler keeps up, which a loaded/slow CI runner doesn't guarantee -- it can undercount pings
    # without the loop actually being broken.
    task = asyncio.ensure_future(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=0.02)
    )
    deadline = time.monotonic() + 5.0
    while len(calls) < _MIN_EXPECTED_PING_CALLS and time.monotonic() < deadline:  # noqa: ASYNC110 -- polling a plain list, not an event
      await asyncio.sleep(0.005)
    task.cancel()
    try:
      await task
    except asyncio.CancelledError:
      pass

    assert len(calls) >= _MIN_EXPECTED_PING_CALLS
    assert calls[0] == (SecretStr("https://hc-ping.com/uuid"), False, True, False)
    assert all(entry == (SecretStr("https://hc-ping.com/uuid"), False, False, False) for entry in calls[1:])

  async def test_stops_immediately_once_fatal_event_is_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: calls.append((a, k)))
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)

    async def set_event_soon() -> None:
      await asyncio.sleep(0.02)
      fake_shutdown.set()

    task = asyncio.create_task(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10)
    )
    asyncio.create_task(set_event_soon())  # noqa: RUF006

    await asyncio.wait_for(task, timeout=1)  # must return well before the 10s interval elapses

    assert calls  # at least the initial start ping was sent


class TestHeartbeatThread:
  def test_stops_immediately_once_fatal_event_is_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Standard library imports
    import time

    calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: calls.append((a, k)))
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10
    )
    time.sleep(0.02)
    t0 = time.monotonic()
    fake_shutdown.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert time.monotonic() - t0 < 1  # well under the 10s interval
    assert calls  # at least the initial start ping was sent

  def test_send_start_false_sends_a_plain_initial_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=10, send_start=False
    )
    fake_shutdown.set()
    thread.join(timeout=2)

    assert calls == [(SecretStr("https://hc-ping.com/uuid"), False, False, False)]

  def test_sends_subsequent_plain_pings_on_the_configured_interval(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[SecretStr | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url=SecretStr("https://hc-ping.com/uuid"), interval=0.02
    )
    # Polled rather than a fixed ~90ms sleep: a fixed budget at a 20ms interval assumes the thread
    # gets scheduled promptly, which a loaded/slow CI runner doesn't guarantee -- it can undercount
    # pings without the heartbeat loop actually being broken.
    assert wait_until(lambda: len(calls) >= _MIN_EXPECTED_PING_CALLS)
    fake_shutdown.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(calls) >= _MIN_EXPECTED_PING_CALLS
    assert calls[0] == (SecretStr("https://hc-ping.com/uuid"), False, True, False)
    assert all(entry == (SecretStr("https://hc-ping.com/uuid"), False, False, False) for entry in calls[1:])


class TestUnderASupervisor:
  async def test_the_periodic_ping_stands_down_but_the_file_is_still_written(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    pings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", aiologic.Event())
    monkeypatch.setenv("DEVKIT_SUPERVISED_PING", "1")
    heartbeat_file = tmp_path / "heartbeat.txt"

    await _run_briefly(heartbeat_module.run_heartbeat_async(heartbeat_file, pingkey=SecretStr("key"), slug="app", interval=0.05), 0.2)

    assert datetime.fromisoformat(heartbeat_file.read_text())
    # ping_healthcheck(None) is its documented no-op: nothing reaches the network.
    assert pings and all(args[0] is None for args, _ in pings), pings

  def test_the_thread_stands_down_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)
    monkeypatch.setenv("DEVKIT_SUPERVISED_PING", "1")
    heartbeat_file = tmp_path / "heartbeat.txt"

    thread = heartbeat_module.start_heartbeat_thread(heartbeat_file, pingkey=SecretStr("key"), slug="app", interval=0.05)
    time.sleep(0.2)
    fake_shutdown.set()
    thread.join(timeout=2)

    assert datetime.fromisoformat(heartbeat_file.read_text())
    # ping_healthcheck(None) is its documented no-op: nothing reaches the network.
    assert pings and all(args[0] is None for args, _ in pings), pings

  def test_a_known_failure_is_still_the_apps_to_send(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda url, *, failure=False, **_: calls.append((url, failure)))
    monkeypatch.setenv("DEVKIT_SUPERVISED_PING", "1")

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey=SecretStr("key"), slug="app", failure=True)

    assert calls == [(SecretStr("https://hc-ping.com/key/app"), True)]

  def test_without_the_variable_the_app_pings_as_before(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    fake_shutdown = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "SHUTDOWN", fake_shutdown)
    monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)

    thread = heartbeat_module.start_heartbeat_thread(tmp_path / "heartbeat.txt", pingkey=SecretStr("key"), slug="app", interval=0.05)
    assert wait_until(lambda: len(pings) >= _MIN_EXPECTED_PING_CALLS)
    fake_shutdown.set()
    thread.join(timeout=2)

    assert len(pings) >= _MIN_EXPECTED_PING_CALLS


_URL = SecretStr("https://hc-ping.com/uuid")


def _judge_later(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
  """Makes the heartbeat judge its checks *seconds* on, so a check stalls without the test waiting for it."""
  real_tick = heartbeat_module.heartbeat_tick
  monkeypatch.setattr(heartbeat_module, "heartbeat_tick", lambda now: real_tick(now + timedelta(seconds=seconds)))


@pytest.mark.usefixtures("health_dir")
class TestHealthCheckRules:
  def test_a_withheld_heartbeat_still_writes_the_file_but_skips_the_ping(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    pings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
    health.declare_health_check("imap", period=600, grace=120, affects_ping=True)
    health.send_health_check("imap", failure=True)
    heartbeat_file = tmp_path / "heartbeat.txt"

    heartbeat_module.send_heartbeat(heartbeat_file, ping_url=_URL)

    assert datetime.fromisoformat(heartbeat_file.read_text())
    assert pings == []
    health.send_health_check("imap")
    heartbeat_module.send_heartbeat(heartbeat_file, ping_url=_URL)
    assert pings == [((_URL,), {"start": False, "failure": False, "autoprovision": False})]

  def test_a_known_failure_ping_is_never_withheld(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda url, *, failure=False, **_: calls.append((url, failure)))
    monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
    health.declare_health_check("imap", period=600, grace=120, affects_ping=True)
    health.send_health_check("imap", failure=True)

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url=_URL, failure=True)

    assert calls == [(_URL, True)]

  def test_stall_alerts_go_out_as_one_fail_in_place_of_the_regular_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
    health.declare_health_check("anchor", period=3600, grace=0)
    health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP server unresponsive", fail_ping=True)
    health.declare_health_check("db", period=60, grace=0, fail_ping=True)
    health.send_health_check("anchor")
    # Judged from the declarations' own time, so the ages in the reasons are exact.
    written_at = health._REGISTRY.written_at  # pyright: ignore[reportPrivateUsage]
    assert written_at is not None
    monkeypatch.setattr(heartbeat_module, "heartbeat_tick", lambda _now: health.heartbeat_tick(written_at + timedelta(seconds=720)))

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url=_URL)
    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url=_URL)

    body = "imap: IMAP server unresponsive (no ping for 720 s)\ndb: no ping for 720 s (period 60 s + grace 0 s)"
    assert pings == [
      ((_URL,), {"failure": True, "autoprovision": False, "body": body}),
      # The stalls are not new on the next tick, and neither check withholds the ping.
      ((_URL,), {"start": False, "failure": False, "autoprovision": False}),
    ]

  def test_a_stalled_shutdown_check_does_not_stop_a_debug_run(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    stops: list[object] = []
    monkeypatch.setattr(heartbeat_module, "run_shutdown", lambda *a, **k: stops.append((a, k)))
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_a, **_k: None)
    monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
    health.declare_health_check("db", period=60, grace=0, stall_message="database gone", shutdown=True)
    health.send_health_check("db")
    _judge_later(monkeypatch, 3600)

    with caplog.at_level(logging.WARNING):
      heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt")

    assert stops == []
    assert any(record.levelno == logging.WARNING and "db: database gone" in record.getMessage() for record in caplog.records)

  def test_supervised_the_rules_are_left_to_the_supervisor_but_every_file_is_written(
    self, tmp_path: Path, health_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    pings: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: pings.append((a, k)))
    stops: list[object] = []
    monkeypatch.setattr(heartbeat_module, "run_shutdown", lambda *a, **k: stops.append((a, k)))
    monkeypatch.setenv("DEVKIT_SUPERVISED_PING", "1")
    health.declare_health_check("heartbeat", period=60, grace=120, affects_ping=True, fail_ping=True)
    health.declare_health_check("db", period=60, grace=0, affects_ping=True, fail_ping=True, shutdown=True)
    health.send_health_check("db", failure=True)
    _judge_later(monkeypatch, 3600)
    heartbeat_file = tmp_path / "heartbeat.txt"

    with caplog.at_level(logging.WARNING):
      heartbeat_module.send_heartbeat(heartbeat_file)

    # Today's ping, neither withheld nor turned into a /fail.
    assert pings == [((None,), {"start": False, "failure": False, "autoprovision": False})]
    # Not even the debug run's "not shutting down" warning: the stall is not aeth-ext's to act on.
    assert stops == []
    assert not [record for record in caplog.records if record.name == heartbeat_module.__name__]
    assert datetime.fromisoformat(heartbeat_file.read_text())
    assert set(json.loads((health_dir / "checks.json").read_text(encoding="utf-8"))["checks"]) == {"heartbeat", "db"}
    assert (health_dir / "state" / "heartbeat.json").exists()
    assert (health_dir / "state" / "db.json").exists()


class TestBuiltinHeartbeatCheck:
  def test_each_heartbeat_pings_the_builtin_check(self, health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_a, **_k: None)
    health.declare_health_check("heartbeat", period=60, grace=120, affects_ping=True, fail_ping=True)

    heartbeat_module.send_heartbeat(None)

    assert (health_dir / "state" / "heartbeat.json").exists()

  def test_a_custom_interval_redeclares_the_builtin_period(self) -> None:
    health.declare_health_check("heartbeat", period=60, grace=120)

    # Never awaited, so closed to keep the output free of "never awaited" warnings.
    heartbeat_module.run_heartbeat_async(None, interval=0.01).close()
    assert health._REGISTRY.declarations["heartbeat"].period == 1  # pyright: ignore[reportPrivateUsage]

    heartbeat_module.HeartbeatThread(None, interval=90.5)
    assert health._REGISTRY.declarations["heartbeat"].period == _CUSTOM_INTERVAL_PERIOD  # pyright: ignore[reportPrivateUsage]

  def test_a_frozen_registry_is_not_redeclared(self, health_dir: Path) -> None:
    del health_dir
    health.declare_health_check("heartbeat", period=60, grace=120)
    health.send_health_check("heartbeat")

    heartbeat_module.HeartbeatThread(None, interval=90.5)

    assert health._REGISTRY.declarations["heartbeat"].period == _BUILTIN_PERIOD  # pyright: ignore[reportPrivateUsage]

  def test_without_the_builtin_check_nothing_is_declared(self) -> None:
    heartbeat_module.HeartbeatThread(None, interval=90.5)

    assert "heartbeat" not in health._REGISTRY.declarations  # pyright: ignore[reportPrivateUsage]
