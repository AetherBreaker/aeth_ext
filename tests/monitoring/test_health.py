# Standard library imports
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

# Third party imports
import pytest

# First party imports
from aeth_ext.monitoring import health
from aeth_ext.settings import BaseSettings

_PERIOD = 600


@pytest.fixture
def health_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
  monkeypatch.setattr(BaseSettings.get_settings(), "log_loc_folder", tmp_path / "logs")
  return tmp_path / "logs" / "health"


def _decl(name: str) -> dict[str, object]:
  return health._REGISTRY.declarations[name].as_json()  # pyright: ignore[reportPrivateUsage]


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


def test_an_empty_stall_message_is_stored_as_none() -> None:
  health.declare_health_check("imap", period=600, grace=120, stall_message="")
  assert _decl("imap")["stall_message"] is None


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
  state = json.loads((health_dir / "state" / "imap.json").read_text(encoding="utf-8"))
  assert state["failure"] is False
  assert state["message"] is None
  assert datetime.fromisoformat(state["at"]) >= written_at
  assert not (health_dir / "state" / "other.json").exists()


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
  state = json.loads((health_dir / "state" / "imap.json").read_text(encoding="utf-8"))
  assert state["failure"] is True
  assert state["message"] == ("a b " + "c" * 2000)[:500]


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
