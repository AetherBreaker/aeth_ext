"""App-declared health checks: a process-wide registry that freezes on the first ping."""

# Standard library imports
import re
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  # Standard library imports
  from datetime import datetime

__all__ = ["HealthCheckError", "declare_health_check"]

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_REQUIRED = frozenset({"period", "grace"})
_FIELD_TYPES: dict[str, type] = {
  "period": int,
  "grace": int,
  "stall_message": str,
  "affects_ping": bool,
  "fail_ping": bool,
  "shutdown": bool,
}
# What `None` voids a non-required field to; also what a new check starts from.
_DEFAULTS: dict[str, object] = {"stall_message": None, "affects_ping": False, "fail_ping": False, "shutdown": False}


class HealthCheckError(RuntimeError):
  """Health-check contract misuse (bad declaration, undeclared name, declaring after the freeze).

  Deliberately uncaught: it is a programming error, so aeth-ext's crash hooks make it fatal and the
  mistake surfaces at once instead of leaving a check that silently never runs.
  """


class _Unset:
  """Type of `_UNSET`, the "argument not given" marker, so `None` stays a distinct, meaningful value."""

  __slots__ = ()


_UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class _Declaration:
  period: int
  grace: int
  stall_message: str | None = None
  affects_ping: bool = False
  fail_ping: bool = False
  shutdown: bool = False

  def as_json(self) -> dict[str, object]:
    """Returns this check's `checks.json` entry."""
    return {
      "period_secs": self.period,
      "grace_secs": self.grace,
      "stall_message": self.stall_message,
      "affects_ping": self.affects_ping,
      "fail_ping": self.fail_ping,
      "shutdown": self.shutdown,
    }


@dataclass(frozen=True, slots=True)
class _Report:
  """The last ping of a check: its state-file content."""

  at: datetime
  failure: bool
  message: str | None


class _Registry:
  """Process-wide health-check state; every read and write of it happens under `lock`."""

  __slots__ = ("declarations", "declarations_written", "frozen", "lock", "reports", "stalled", "written_at")

  def __init__(self) -> None:
    self.lock = threading.Lock()
    self.declarations: dict[str, _Declaration] = {}
    self.frozen = False
    self.written_at: datetime | None = None
    self.declarations_written = False
    self.reports: dict[str, _Report] = {}
    self.stalled: dict[str, bool] = {}


_REGISTRY = _Registry()


def declare_health_check(
  name: str,
  *,
  period: int | _Unset = _UNSET,
  grace: int | _Unset = _UNSET,
  stall_message: str | _Unset | None = _UNSET,
  affects_ping: bool | _Unset | None = _UNSET,
  fail_ping: bool | _Unset | None = _UNSET,
  shutdown: bool | _Unset | None = _UNSET,
) -> None:
  """Declares a health check, or overwrites the given values of an already declared one.

  A new name needs `period` and `grace` (whole seconds, `period >= 1`, `grace >= 0`); the rest default
  to no stall message and every flag off. Re-declaring overwrites only what is passed. An explicit
  `None` voids a value to the parameter's own default (not to the earlier declaration); `period` and
  `grace` have none, so they cannot be voided. The registry freezes on the first ping.

  Args:
    name: Lowercase `[a-z0-9][a-z0-9_-]{0,63}`, since it becomes a file name.
    period: Seconds between the pings the check expects.
    grace: Seconds past `period` before the check counts as stalled.
    stall_message: Reason shown when the check stalls; empty is the same as `None`.
    affects_ping: Whether a stalled check suppresses the regular success ping.
    fail_ping: Whether a stalled check sends a failure ping.
    shutdown: Whether a stalled check shuts the app down.

  Raises:
    HealthCheckError: On an invalid name or value, a missing `period`/`grace` for a new check, or a
      declaration after the registry froze.
  """
  given = {
    key: value
    for key, value in (
      ("period", period),
      ("grace", grace),
      ("stall_message", stall_message),
      ("affects_ping", affects_ping),
      ("fail_ping", fail_ping),
      ("shutdown", shutdown),
    )
    if not isinstance(value, _Unset)
  }
  if not _NAME_RE.fullmatch(name):
    raise HealthCheckError(f"invalid health check name {name!r}: must match {_NAME_RE.pattern}")
  for key, value in given.items():
    kind = _FIELD_TYPES[key]
    if value is None:
      if key in _REQUIRED:
        raise HealthCheckError(f"{name}: {key} cannot be voided")
    # bool is an int subclass, so a bool period/grace needs its own exclusion.
    elif not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
      raise HealthCheckError(f"{name}: {key} must be {kind.__name__}, got {value!r}")

  with _REGISTRY.lock:
    if _REGISTRY.frozen:
      raise HealthCheckError(f"{name}: health checks cannot be declared after the first ping froze the registry")
    previous = _REGISTRY.declarations.get(name)
    values: dict[str, Any] = asdict(previous) if previous else {}
    # `None` and an empty stall message both fall back to the parameter's own default.
    values.update({key: value or _DEFAULTS.get(key, value) for key, value in given.items()})
    if not _REQUIRED <= values.keys():
      raise HealthCheckError(f"{name}: a new health check needs both period and grace")
    if values["period"] < 1 or values["grace"] < 0:
      raise HealthCheckError(f"{name}: period must be >= 1 and grace >= 0, got {values['period']} and {values['grace']}")
    _REGISTRY.declarations[name] = _Declaration(**{**_DEFAULTS, **values})
