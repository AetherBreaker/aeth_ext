# Standard library imports
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest
from pydantic import SecretStr

# First party imports
from aeth_ext.monitoring import health
from aeth_ext.settings import BaseSettings

# Local imports
from tests.conftest import wait_until

if TYPE_CHECKING:
  # First party imports
  from tests.monitoring.conftest import FailPing

_PERIOD = 600
_SCENARIOS_SCRIPT = Path(__file__).parent / "_optimized_scenarios.py"


def _decl(name: str) -> dict[str, object]:
  return health._REGISTRY.declarations[name].as_json()  # pyright: ignore[reportPrivateUsage]


def _pinged_at(name: str) -> datetime:
  return health._REGISTRY.reports[name].at  # pyright: ignore[reportPrivateUsage]


def _written_at() -> datetime:
  written_at = health._REGISTRY.written_at  # pyright: ignore[reportPrivateUsage]
  assert written_at is not None
  return written_at


def _state(health_dir: Path, name: str) -> dict[str, Any]:
  return json.loads((health_dir / "state" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def hc_settings(monkeypatch: pytest.MonkeyPatch) -> None:
  """A ping-key/slug URL in the live settings, which is what a failure ping's ``/fail`` resolves."""
  settings = BaseSettings.get_settings()
  monkeypatch.setattr(settings, "alerts_healthcheck_ping_url", None)
  monkeypatch.setattr(settings, "alerts_healthcheck_pingkey", SecretStr("key"))
  monkeypatch.setattr(settings, "heartbeat_slug", "app")


def test_a_new_check_takes_its_values_and_the_defaults() -> None:
  health.declare_health_check("imap", period=600, grace=120)
  assert _decl("imap") == {
    "period_secs": 600,
    "grace_secs": 120,
    "stall_message": None,
    "affects_ping": False,
    "fail_ping": False,
    "shutdown": False,
  }


def test_redeclaring_overwrites_only_what_is_passed() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP server unresponsive", fail_ping=True)
  health.declare_health_check("imap", grace=30)
  assert _decl("imap") == {
    "period_secs": 600,
    "grace_secs": 30,
    "stall_message": "IMAP server unresponsive",
    "affects_ping": False,
    "fail_ping": True,
    "shutdown": False,
  }


def test_none_voids_to_the_parameters_own_default() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="x", affects_ping=True)
  health.declare_health_check("imap", stall_message=None, affects_ping=None)
  assert (_decl("imap")["stall_message"], _decl("imap")["affects_ping"]) == (None, False)


@pytest.mark.parametrize("stall_message", ["", " \n\t "])
def test_an_empty_stall_message_is_stored_as_none(stall_message: str) -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message=stall_message)
  assert _decl("imap")["stall_message"] is None


def test_the_stall_message_is_normalised_when_declared() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP\n  server " + "x" * 600)
  assert _decl("imap")["stall_message"] == ("IMAP server " + "x" * 600)[:500]


@pytest.mark.parametrize("kwargs", [{"period": None}, {"grace": None}])
def test_period_and_grace_cannot_be_voided(kwargs: dict[str, None]) -> None:
  health.declare_health_check("imap", period=600, grace=120)
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", **kwargs)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
  "kwargs",
  [
    {},
    {"period": 600},
    {"grace": 120},
    {"period": 0, "grace": 1},
    {"period": 1, "grace": -1},
    {"period": 31536001, "grace": 0},
    {"period": 1, "grace": 31536001},
  ],
)
def test_a_new_check_needs_a_valid_period_and_grace(kwargs: dict[str, int]) -> None:
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", **kwargs)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("name", ["../x", "Imap", "", "a/b", "a" * 65])
def test_invalid_names_are_refused(name: str) -> None:
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check(name, period=1, grace=0)


@pytest.mark.parametrize(
  "kwargs",
  [
    {"period": 1.5, "grace": 0},
    {"period": True, "grace": 0},
    {"period": 1, "grace": False},
    {"period": "1", "grace": 0},
    {"period": 1, "grace": 0, "affects_ping": 1},
    {"period": 1, "grace": 0, "fail_ping": "yes"},
    {"period": 1, "grace": 0, "shutdown": 0},
    {"period": 1, "grace": 0, "stall_message": 5},
  ],
)
def test_wrong_types_are_refused(kwargs: dict[str, object]) -> None:
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", **kwargs)  # pyright: ignore[reportArgumentType]


def test_a_refused_redeclaration_leaves_the_check_unchanged() -> None:
  health.declare_health_check("imap", period=_PERIOD, grace=120)
  with pytest.raises(health.HealthCheckError):
    health.declare_health_check("imap", period=5, grace=-1)
  assert _decl("imap")["period_secs"] == _PERIOD


def test_pinging_an_undeclared_check_raises(health_dir: Path) -> None:
  with pytest.raises(health.HealthCheckError):
    health.send_health_check("imap")
  assert not health_dir.exists()


def test_the_first_ping_freezes_and_writes_the_declarations(health_dir: Path) -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP stuck", fail_ping=True)
  health.declare_health_check("other", period=60, grace=0)
  health.send_health_check("imap")
  checks = json.loads((health_dir / "checks.json").read_text(encoding="utf-8"))
  assert checks["version"] == 1
  assert checks["checks"] == {"imap": _decl("imap"), "other": _decl("other")}
  written_at = datetime.fromisoformat(checks["written_at"])
  assert written_at.utcoffset() is not None
  # The first ping carries the freeze's own stamp, and no failure has happened yet.
  assert _state(health_dir, "imap") == {"at": checks["written_at"], "failure": False, "message": None, "last_failure": None}
  assert not (health_dir / "state" / "other.json").exists()


def test_the_state_file_keeps_the_runs_last_failure_ping(health_dir: Path) -> None:
  health.declare_health_check("imap", period=600, grace=120)
  health.send_health_check("imap", failure=True, message="mailbox\n unreadable")
  failed = _state(health_dir, "imap")
  assert failed == {
    "at": failed["at"],
    "failure": True,
    "message": "mailbox unreadable",
    "last_failure": {"at": failed["at"], "message": "mailbox unreadable"},
  }
  # A success ping overwrites the latest ping but keeps the failure for the supervisor's next poll.
  health.send_health_check("imap")
  kept = _state(health_dir, "imap")
  assert (kept["failure"], kept["message"], kept["last_failure"]) == (False, None, failed["last_failure"])
  health.send_health_check("imap", failure=True)
  replaced = _state(health_dir, "imap")
  assert replaced["last_failure"] == {"at": replaced["at"], "message": None}


@pytest.mark.usefixtures("health_dir")
def test_declaring_after_the_freeze_raises() -> None:
  health.declare_health_check("imap", period=600, grace=120)
  health.send_health_check("imap")
  for name in ("imap", "new"):
    with pytest.raises(health.HealthCheckError):
      health.declare_health_check(name, period=600, grace=120)


def test_a_failure_ping_stores_a_single_capped_line(health_dir: Path) -> None:
  health.declare_health_check("imap", period=600, grace=120)
  health.send_health_check("imap", failure=True, message="a\n  b\t" + "c" * 2000)
  state = _state(health_dir, "imap")
  assert state["failure"] is True
  assert state["message"] == ("a b " + "c" * 2000)[:500]


@pytest.mark.usefixtures("hc_settings")
def test_a_lone_surrogate_is_written_and_sent_as_valid_utf8(
  health_dir: Path, monkeypatch: pytest.MonkeyPatch, fail_pings: list[FailPing]
) -> None:
  monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
  health.declare_health_check("imap", period=600, grace=120, stall_message="stuck \udc80")
  health.send_health_check("imap", failure=True, message="a \udc80 b")
  checks = json.loads((health_dir / "checks.json").read_text(encoding="utf-8"))
  state = _state(health_dir, "imap")
  # serde_json refuses a lone surrogate's `\udcXX` escape; a strict encode refuses the surrogate itself.
  json.dumps([checks, state], ensure_ascii=False).encode("utf-8")
  assert checks["checks"]["imap"]["stall_message"] == "stuck ?"
  assert state["message"] == state["last_failure"]["message"] == "a ? b"
  assert wait_until(lambda: len(fail_pings) >= 1)
  assert fail_pings[0][3] == "imap: a ? b"


@pytest.mark.parametrize("kwargs", [{"failure": None}, {"failure": 1}, {"failure": ["x"]}, {"message": 5}, {"message": b"x"}])
def test_a_non_bool_failure_or_non_str_message_is_refused_before_any_write_or_freeze(
  health_dir: Path, kwargs: dict[str, object]
) -> None:
  health.declare_health_check("imap", period=600, grace=120)
  with pytest.raises(health.HealthCheckError):
    health.send_health_check("imap", **kwargs)  # pyright: ignore[reportArgumentType]
  assert not health_dir.exists()
  health.declare_health_check("imap", grace=30)


def test_the_folders_are_created_and_write_errors_are_logged_not_raised(
  health_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
  health.declare_health_check("imap", period=600, grace=120)

  def deny(*_args: object, **_kwargs: object) -> Path:
    raise PermissionError("denied")

  with monkeypatch.context() as patch:
    patch.setattr(Path, "replace", deny)
    with caplog.at_level(logging.ERROR):
      health.send_health_check("imap")
  assert any(record.levelno == logging.ERROR for record in caplog.records)
  assert not (health_dir / "checks.json").exists()
  health.send_health_check("imap")
  assert (health_dir / "checks.json").exists()
  assert (health_dir / "state" / "imap.json").exists()


def test_concurrent_first_pings_write_the_declarations_once(health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
  health.declare_health_check("a", period=600, grace=120)
  health.declare_health_check("b", period=600, grace=120)
  real_replace = Path.replace
  writes: list[Path] = []

  def counting_replace(self: Path, target: Path) -> Path:
    if Path(target).name == "checks.json":
      writes.append(Path(target))
    return real_replace(self, target)

  monkeypatch.setattr(Path, "replace", counting_replace)
  barrier = threading.Barrier(2)

  def ping(name: str) -> None:
    barrier.wait()
    health.send_health_check(name)

  threads = [threading.Thread(target=ping, args=(name,)) for name in ("a", "b")]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  assert len(writes) == 1
  assert (health_dir / "state" / "a.json").exists()
  assert (health_dir / "state" / "b.json").exists()


@pytest.mark.usefixtures("health_dir")
def test_heartbeat_tick_judges_with_devkit_containers_strings() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="IMAP server unresponsive", fail_ping=True)
  health.send_health_check("imap")
  t0 = _pinged_at("imap")
  assert health.heartbeat_tick(t0 + timedelta(seconds=719)) == health.Tick(withheld=False, alerts=[], stop=None)
  assert health.heartbeat_tick(t0 + timedelta(seconds=720)).alerts == ["imap: IMAP server unresponsive (no ping for 720 s)"]
  # One alert per stall episode.
  assert health.heartbeat_tick(t0 + timedelta(seconds=780)).alerts == []
  health.send_health_check("imap")
  t1 = _pinged_at("imap")
  assert health.heartbeat_tick(t1).alerts == []
  assert health.heartbeat_tick(t1 + timedelta(seconds=725)).alerts == ["imap: IMAP server unresponsive (no ping for 725 s)"]


@pytest.mark.usefixtures("health_dir")
def test_only_flagged_checks_alert_or_stop_and_unpinged_checks_stall_from_the_declarations() -> None:
  health.declare_health_check("plain", period=60, grace=0)
  health.declare_health_check("alerting", period=60, grace=0, fail_ping=True)
  # Judged by the stall message as `checks.json` carries it: collapsed to one line.
  health.declare_health_check("db", period=60, grace=0, stall_message="database\n  gone", shutdown=True)
  health.declare_health_check("later", period=60, grace=0, shutdown=True)
  health.send_health_check("plain")
  written_at = _written_at()
  assert health.heartbeat_tick(written_at + timedelta(seconds=120)) == health.Tick(
    withheld=False,
    alerts=["alerting: no ping for 120 s (period 60 s + grace 0 s)"],
    stop="db: database gone (no ping for 120 s)",
  )
  assert health.heartbeat_tick(written_at + timedelta(seconds=180)) == health.Tick(withheld=False, alerts=[], stop=None)


@pytest.mark.usefixtures("health_dir")
def test_alerts_and_the_stop_follow_name_order_as_devkit_containers_do() -> None:
  # Declared against name order, so a judgment in declaration order would put "zeta" first.
  health.declare_health_check("zeta", period=60, grace=0, fail_ping=True, shutdown=True)
  health.declare_health_check("alpha", period=60, grace=0, fail_ping=True, shutdown=True)
  health.declare_health_check("anchor", period=3600, grace=0)
  health.send_health_check("anchor")
  reason = "{}: no ping for 60 s (period 60 s + grace 0 s)"
  assert health.heartbeat_tick(_written_at() + timedelta(seconds=60)) == health.Tick(
    withheld=False, alerts=[reason.format("alpha"), reason.format("zeta")], stop=reason.format("alpha")
  )


@pytest.mark.usefixtures("health_dir")
def test_affects_ping_withholds_while_failing_or_stalled() -> None:
  health.declare_health_check("imap", period=600, grace=120, affects_ping=True)
  health.declare_health_check("other", period=60, grace=0)
  health.send_health_check("imap")
  assert not health.heartbeat_tick(_pinged_at("imap")).withheld
  # A check without `affects_ping` never withholds, failing or stalled.
  health.send_health_check("other", failure=True)
  assert not health.heartbeat_tick(_pinged_at("other") + timedelta(seconds=120)).withheld
  health.send_health_check("imap", failure=True, message="mailbox unreadable")
  assert health.heartbeat_tick(_pinged_at("imap")).withheld
  health.send_health_check("imap")
  assert not health.heartbeat_tick(_pinged_at("imap")).withheld
  assert health.heartbeat_tick(_pinged_at("imap") + timedelta(seconds=720)).withheld


def test_heartbeat_tick_is_a_no_op_before_the_freeze_and_pings_the_built_in_check(health_dir: Path) -> None:
  health.declare_health_check("imap", period=1, grace=0, fail_ping=True, shutdown=True)
  assert health.heartbeat_tick(datetime.now(UTC) + timedelta(days=1)) == health.Tick(withheld=False, alerts=[], stop=None)
  assert not health_dir.exists()
  health.declare_health_check("heartbeat", period=60, grace=120)
  assert health.heartbeat_tick(datetime.now(UTC)) == health.Tick(withheld=False, alerts=[], stop=None)
  assert (health_dir / "checks.json").exists()
  assert _state(health_dir, "heartbeat")["failure"] is False


@pytest.mark.parametrize(
  ("message", "body"),
  [("mailbox\n  unreadable", "imap: mailbox unreadable"), (None, "imap: failure ping"), (" \n ", "imap: failure ping")],
)
@pytest.mark.usefixtures("health_dir", "hc_settings")
def test_unsupervised_failure_pings_post_the_reason(
  message: str | None, body: str, monkeypatch: pytest.MonkeyPatch, fail_pings: list[FailPing]
) -> None:
  monkeypatch.delenv("DEVKIT_SUPERVISED_PING", raising=False)
  health.declare_health_check("imap", period=600, grace=120)
  health.send_health_check("imap")
  health.send_health_check("imap", failure=True, message=message)
  assert wait_until(lambda: len(fail_pings) >= 1)
  assert fail_pings == [(SecretStr("https://hc-ping.com/key/app"), True, True, body)]


@pytest.mark.usefixtures("hc_settings")
def test_supervised_failure_pings_send_nothing(health_dir: Path, monkeypatch: pytest.MonkeyPatch, fail_pings: list[FailPing]) -> None:
  monkeypatch.setenv("DEVKIT_SUPERVISED_PING", "1")
  health.declare_health_check("imap", period=600, grace=120)
  health.send_health_check("imap", failure=True, message="mailbox unreadable")
  assert (health_dir / "checks.json").exists()
  assert _state(health_dir, "imap")["failure"] is True
  # The /fail would run on a thread, so give it the time it would need to show up.
  assert not wait_until(lambda: bool(fail_pings), timeout=0.2)


def test_a_stalled_shutdown_check_stops_an_optimized_run() -> None:
  env = {key: value for key, value in os.environ.items() if key != "DEVKIT_SUPERVISED_PING"}
  env.setdefault("ALERTS_EMAIL_PWD", "test-password")
  proc = subprocess.run(
    [sys.executable, "-O", str(_SCENARIOS_SCRIPT), "stalled_shutdown_check_stops_the_app"],
    capture_output=True,
    text=True,
    env=env,
    timeout=30,
    check=False,
  )
  assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
  assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"kinds": [1]}
