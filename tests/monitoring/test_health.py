# Third party imports
import pytest

# First party imports
from aeth_ext.monitoring import health

_PERIOD = 600


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


@pytest.mark.parametrize("kwargs", [{}, {"period": 600}, {"grace": 120}, {"period": 0, "grace": 1}, {"period": 1, "grace": -1}])
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
