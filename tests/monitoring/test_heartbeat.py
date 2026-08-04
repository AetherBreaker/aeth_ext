"""Tests for `aeth_ext.monitoring.heartbeat`.

`run_heartbeat_async` and `HeartbeatThread`/`start_heartbeat_thread` both
reference `FATAL_EVENT` as a plain module-level name imported into
`heartbeat.py`, so every test here that needs to control it monkeypatches
`heartbeat_module.FATAL_EVENT` to a fresh, throwaway `aiologic.Event()`
instance rather than touching the real process-wide, one-shot
`aeth_ext.errors.FATAL_EVENT` -- which the root `conftest.py`'s
`_clear_fatal_event` fixture asserts stays unset for the whole session.
"""

# Standard library imports
import asyncio
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

# Third party imports
import aiologic

# First party imports
from aeth_ext.monitoring import heartbeat as heartbeat_module

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Coroutine
  from pathlib import Path
  from typing import Any

  # Third party imports
  import pytest

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


class TestSendHeartbeat:
  def test_writes_current_timestamp_to_the_heartbeat_file(self, tmp_path: Path):
    heartbeat_file = tmp_path / "heartbeat.txt"

    heartbeat_module.send_heartbeat(heartbeat_file)

    written = heartbeat_file.read_text()
    assert datetime.fromisoformat(written)

  def test_none_heartbeat_file_skips_the_local_write(self, monkeypatch: pytest.MonkeyPatch):
    ping_calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: ping_calls.append((a, k)))

    heartbeat_module.send_heartbeat(None)  # must not raise

  def test_write_failure_is_logged_not_raised(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
    missing_dir_file = tmp_path / "does_not_exist" / "heartbeat.txt"

    with caplog.at_level("ERROR"):
      heartbeat_module.send_heartbeat(missing_dir_file)  # must not raise

    assert any("Failed to write heartbeat file" in record.message for record in caplog.records)

  def test_ping_url_takes_precedence_over_pingkey_and_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(
      tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/fixed-uuid", pingkey="key", slug="my-app"
    )

    assert calls == [("https://hc-ping.com/fixed-uuid", False, False, False)]

  def test_pingkey_and_slug_build_the_autoprovisioning_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key", slug="my-app")

    assert calls == [("https://hc-ping.com/my-ping-key/my-app", False, False, True)]

  def test_no_ping_configuration_results_in_a_none_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt")

    assert calls == [(None, False, False, False)]

  def test_auto_detects_slug_from_the_callers_own_frame_when_omitted(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    seen_caller_files: list[str] = []

    def fake_auto_slug(caller_file: str) -> str | None:
      seen_caller_files.append(caller_file)
      return "detected-slug"

    monkeypatch.setattr(heartbeat_module, "_auto_slug", fake_auto_slug)

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key")

    assert calls == [("https://hc-ping.com/my-ping-key/detected-slug", False, False, True)]
    # The frame handed to auto-detection must be *this test file*, not
    # heartbeat.py's own module -- that's the whole bug being fixed.
    assert seen_caller_files == [__file__]

  def test_explicit_slug_skips_auto_detection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: None)
    auto_slug_calls: list[str] = []
    monkeypatch.setattr(heartbeat_module, "_auto_slug", auto_slug_calls.append)

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key", slug="explicit-slug")

    assert auto_slug_calls == []

  def test_auto_slug_lookup_is_cached_per_caller_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: None)
    heartbeat_module._auto_slug.cache_clear()  # pyright: ignore[reportPrivateUsage]
    lookup_calls: list[str] = []

    def fake_parse_and_grab_constants(*, expected_constants: dict[str, str], caller_file: str) -> dict[str, str]:
      del expected_constants
      lookup_calls.append(caller_file)
      return {"heartbeat_slug": "cached-slug"}

    monkeypatch.setattr(heartbeat_module, "parse_and_grab_constants", fake_parse_and_grab_constants)

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key")
    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key")
    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", pingkey="my-ping-key")

    # Same caller file across all three calls -- the underlying AST-based
    # lookup must only run once, not once per heartbeat.
    assert lookup_calls == [__file__]

  def test_start_and_failure_flags_are_forwarded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )

    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", start=True)
    heartbeat_module.send_heartbeat(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", failure=True)

    assert calls == [
      ("https://hc-ping.com/uuid", False, True, False),
      ("https://hc-ping.com/uuid", True, False, False),
    ]


class TestSendHeartbeatAsync:
  """The asyncio-safe counterpart of `send_heartbeat`.

  Both halves of a heartbeat block -- the file write, and a synchronous
  `urlopen` whose timeout does not cover DNS resolution -- so running them on
  an event loop stalls every task on it, and a wedged resolver stalls them
  indefinitely. That is a live production hazard for the central log server,
  whose reader loop is the same loop that accepts every client connection.
  """

  async def test_runs_the_blocking_work_off_the_event_loop_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ping_threads: list[int] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda _url, **_kwargs: ping_threads.append(threading.get_ident()),
    )

    await heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid")

    assert ping_threads
    assert threading.get_ident() not in ping_threads

  async def test_a_blocking_ping_does_not_stall_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    monkeypatch.setattr(
      heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: release.wait(timeout=_HELD_PING_SECONDS)
    )

    heartbeat = asyncio.ensure_future(
      heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid")
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

  async def test_auto_detects_slug_from_the_callers_own_frame_when_omitted(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ):
    """Resolution must happen *before* the work is handed to a worker thread.

    `_auto_slug` walks the call stack, and the thread `to_thread` runs the
    blocking primitive on no longer has this caller on it -- so resolving late
    would silently produce the wrong slug (or none).
    """
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    seen_caller_files: list[str] = []

    def fake_auto_slug(caller_file: str) -> str | None:
      seen_caller_files.append(caller_file)
      return "detected-slug"

    monkeypatch.setattr(heartbeat_module, "_auto_slug", fake_auto_slug)

    await heartbeat_module.send_heartbeat_async(tmp_path / "heartbeat.txt", pingkey="my-ping-key")

    assert calls == [("https://hc-ping.com/my-ping-key/detected-slug", False, False, True)]
    assert seen_caller_files == [__file__]

  async def test_writes_the_heartbeat_file_and_forwards_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    heartbeat_file = tmp_path / "heartbeat.txt"

    await heartbeat_module.send_heartbeat_async(heartbeat_file, ping_url="https://hc-ping.com/uuid", start=True)

    assert calls == [("https://hc-ping.com/uuid", False, True, False)]
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
  """Uses task.cancel() (not FATAL_EVENT) to stop the loop after a short
  observation window -- exercises the periodic-tick mechanics in isolation
  from the FATAL_EVENT-driven stop behavior, which has its own tests below."""

  async def test_sends_a_start_ping_immediately(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10),
      0.01,
    )

    assert calls == [("https://hc-ping.com/uuid", False, True, False)]

  async def test_runs_the_blocking_work_off_the_event_loop_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ping_threads: list[int] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda _url, **_kwargs: ping_threads.append(threading.get_ident()),
    )
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10),
      0.05,
    )

    assert ping_threads
    assert threading.get_ident() not in ping_threads

  async def test_a_blocking_ping_does_not_stall_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A wedged ping must cost the heartbeat, never the loop it runs on.

    ``urlopen``'s timeout does not cover DNS resolution, so this is the real
    production failure mode: the ping never returns. The loop -- which in the
    central log server also accepts every client connection -- has to keep
    running regardless.
    """
    release = threading.Event()
    monkeypatch.setattr(
      heartbeat_module, "ping_healthcheck", lambda *_args, **_kwargs: release.wait(timeout=_HELD_PING_SECONDS)
    )
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", aiologic.Event())

    task = asyncio.ensure_future(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=0.01)
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

  async def test_send_start_false_sends_a_plain_initial_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(
        tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10, send_start=False
      ),
      0.01,
    )

    assert calls == [("https://hc-ping.com/uuid", False, False, False)]

  async def test_sends_subsequent_plain_pings_on_the_configured_interval(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", aiologic.Event())

    await _run_briefly(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=0.02),
      0.09,
    )

    # 1 start ping + at least 2 subsequent plain pings in ~90ms at a 20ms interval.
    assert len(calls) >= _MIN_EXPECTED_PING_CALLS
    assert calls[0] == ("https://hc-ping.com/uuid", False, True, False)
    assert all(entry == ("https://hc-ping.com/uuid", False, False, False) for entry in calls[1:])

  async def test_stops_immediately_once_fatal_event_is_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: calls.append((a, k)))
    fake_fatal_event = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", fake_fatal_event)

    async def set_event_soon() -> None:
      await asyncio.sleep(0.02)
      fake_fatal_event.set()

    task = asyncio.create_task(
      heartbeat_module.run_heartbeat_async(tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10)
    )
    asyncio.create_task(set_event_soon())  # noqa: RUF006

    await asyncio.wait_for(task, timeout=1)  # must return well before the 10s interval elapses

    assert calls  # at least the initial start ping was sent


class TestHeartbeatThread:
  def test_stops_immediately_once_fatal_event_is_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Standard library imports
    import time

    calls: list[object] = []
    monkeypatch.setattr(heartbeat_module, "ping_healthcheck", lambda *a, **k: calls.append((a, k)))
    fake_fatal_event = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", fake_fatal_event)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10
    )
    time.sleep(0.02)
    t0 = time.monotonic()
    fake_fatal_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert time.monotonic() - t0 < 1  # well under the 10s interval
    assert calls  # at least the initial start ping was sent

  def test_send_start_false_sends_a_plain_initial_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    fake_fatal_event = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", fake_fatal_event)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=10, send_start=False
    )
    fake_fatal_event.set()
    thread.join(timeout=2)

    assert calls == [("https://hc-ping.com/uuid", False, False, False)]

  def test_sends_subsequent_plain_pings_on_the_configured_interval(
    self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ):
    # Standard library imports
    import time

    calls: list[tuple[str | None, bool, bool, bool]] = []
    monkeypatch.setattr(
      heartbeat_module,
      "ping_healthcheck",
      lambda url, *, failure=False, start=False, autoprovision=False: calls.append((url, failure, start, autoprovision)),
    )
    fake_fatal_event = aiologic.Event()
    monkeypatch.setattr(heartbeat_module, "FATAL_EVENT", fake_fatal_event)

    thread = heartbeat_module.start_heartbeat_thread(
      tmp_path / "heartbeat.txt", ping_url="https://hc-ping.com/uuid", interval=0.02
    )
    time.sleep(0.09)
    fake_fatal_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    # 1 start ping + at least 2 subsequent plain pings in ~90ms at a 20ms interval.
    assert len(calls) >= _MIN_EXPECTED_PING_CALLS
    assert calls[0] == ("https://hc-ping.com/uuid", False, True, False)
    assert all(entry == ("https://hc-ping.com/uuid", False, False, False) for entry in calls[1:])
