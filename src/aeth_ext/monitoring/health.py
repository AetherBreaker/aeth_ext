"""App-declared health checks: a process-wide registry that freezes on the first ping."""

# Standard library imports
import json
import math
import re
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from logging import getLogger
from typing import TYPE_CHECKING, Any, NamedTuple

# First party imports
from aeth_ext.monitoring.ping import ping_healthcheck, resolve_ping_url, supervised_ping
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

logger = getLogger(__name__)

__all__ = ["HealthCheckError", "declare_health_check", "send_health_check"]

# devkit-container's `MAX_SECS`: it refuses a `checks.json` whose period or grace exceeds this.
_MAX_SECS = 31_536_000
_MAX_MESSAGE_LEN = 500
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


def _normalize_message(text: str) -> str | None:
  r"""A message as every reader shows it: one line, valid UTF-8, at most 500 characters; empty is `None`.

  A lone surrogate becomes `?`: `json.dumps` would write it as a `\udcXX` escape that serde_json rejects,
  and encoding it for a `/fail` body raises.
  """
  return " ".join(text.split()).encode("utf-8", "replace").decode("utf-8")[:_MAX_MESSAGE_LEN] or None


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

  __slots__ = ("declarations", "declarations_written", "last_failures", "lock", "reports", "stalled", "written_at")

  def __init__(self) -> None:
    self.lock = threading.Lock()
    self.declarations: dict[str, _Declaration] = {}
    # Stamped by the first ping, which freezes the registry: `None` exactly while it is open.
    self.written_at: datetime | None = None
    self.declarations_written = False
    self.reports: dict[str, _Report] = {}
    # Each check's latest failure ping this run, which later success pings leave in place.
    self.last_failures: dict[str, _Report] = {}
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

  `initialize()` declares a built-in `heartbeat` check (period 60 s, grace 120 s, `affects_ping` and
  `fail_ping` on), which every heartbeat pings; opt out with `initialize(heartbeat_check=False)`. To
  adjust it, re-declare `heartbeat` after `initialize()` with just the values to change, or declare it
  in full before `initialize()`, which then keeps it. A heartbeat scheduler with a custom interval sets
  its period.

  Args:
    name: Lowercase `[a-z0-9][a-z0-9_-]{0,63}`, since it becomes a file name.
    period: Seconds between the pings the check expects.
    grace: Seconds past `period` before the check counts as stalled.
    stall_message: Reason shown when the check stalls; normalised like a ping's message, and empty is
      the same as `None`.
    affects_ping: Whether the regular ping is withheld while the check is failing (stalled or its
      latest ping was a failure ping).
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
  if isinstance(stall_message, str):
    # Once, here, so `checks.json` and aeth-ext's own judgment carry the same text.
    given["stall_message"] = _normalize_message(stall_message)

  with _REGISTRY.lock:
    if _REGISTRY.written_at is not None:
      raise HealthCheckError(f"{name}: health checks cannot be declared after the first ping froze the registry")
    previous = _REGISTRY.declarations.get(name)
    values: dict[str, Any] = asdict(previous) if previous else {}
    # `None` voids to the parameter's default; a required key's `None` was refused above.
    values.update({key: _DEFAULTS[key] if value is None else value for key, value in given.items()})
    if not _REQUIRED <= values.keys():
      raise HealthCheckError(f"{name}: a new health check needs both period and grace")
    if not 1 <= values["period"] <= _MAX_SECS or not 0 <= values["grace"] <= _MAX_SECS:
      raise HealthCheckError(
        f"{name}: period must be in 1..{_MAX_SECS} and grace in 0..{_MAX_SECS}, got {values['period']} and {values['grace']}"
      )
    _REGISTRY.declarations[name] = _Declaration(**{**_DEFAULTS, **values})


def declare_builtin_heartbeat_check(period: int) -> None:
  """Declares `initialize()`'s built-in `heartbeat` check, unless a `heartbeat` declaration exists already.

  A second `initialize()` is then harmless after the freeze, and an app's earlier declaration is kept
  whole. Grace 120 s keeps today's 180 s heartbeat max age at the default 60 s period.

  Raises:
    HealthCheckError: If no `heartbeat` was declared before the registry froze.
  """
  with _REGISTRY.lock:
    if "heartbeat" in _REGISTRY.declarations:
      return
    if _REGISTRY.written_at is not None:
      raise HealthCheckError("heartbeat: health checks cannot be declared after the first ping froze the registry")
    _REGISTRY.declarations["heartbeat"] = _Declaration(period=period, grace=120, affects_ping=True, fail_ping=True)


def adopt_heartbeat_interval(interval: float) -> None:
  """Sets the built-in `heartbeat` check's period to a scheduler's interval; a no-op once frozen or if undeclared.

  One locked section, writing the declaration directly: `declare_health_check` takes this non-reentrant
  lock, and releasing it in between would let a ping on another thread freeze the registry first.
  """
  # At least 1: sub-second test intervals would round to an invalid 0. At most devkit-container's limit.
  period = min(_MAX_SECS, max(1, math.ceil(interval)))
  with _REGISTRY.lock:
    builtin = _REGISTRY.declarations.get("heartbeat")
    if builtin is None or _REGISTRY.written_at is not None or builtin.period == period:
      return
    _REGISTRY.declarations["heartbeat"] = replace(builtin, period=period)


def _write_atomic(path: Path, payload: dict[str, object]) -> bool:
  """Writes `payload` as JSON via a sibling temp file and `replace`; returns whether it landed."""
  tmp = path.with_suffix(path.suffix + ".tmp")
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
  except OSError:
    logger.exception("Could not write health file %s", path)
    return False
  return True


def send_health_check(name: str, *, failure: bool = False, message: str | None = None) -> None:
  """Records a ping of a declared health check and writes its state file for devkit-container.

  Blocks briefly on file IO: the state file, plus `checks.json` on the first ping, which freezes the
  registry; that write is retried on later pings if it failed. File errors are logged, not raised. A
  failure ping also sends a healthchecks.io `/fail` with its reason from a daemon thread, so the caller
  never blocks on the network; not when supervised (the supervisor sends it from the state file) or when
  no ping URL is configured.

  Args:
    name: A declared check.
    failure: Whether this ping reports a failure.
    message: Failure detail; collapsed to one line, made valid UTF-8 and capped at 500 characters.

  Raises:
    HealthCheckError: If `name` was never declared, `failure` isn't a `bool`, or `message` isn't a `str`
      or `None`.
  """
  # Refused, not coerced: devkit-container reads a non-boolean `failure` as an unreadable state file, while
  # truthiness here would judge it otherwise; a non-str message would fail in the normaliser.
  if not isinstance(failure, bool) or not isinstance(message, str | None):  # pyright: ignore[reportUnnecessaryIsInstance]
    raise HealthCheckError(f"{name}: failure must be bool and message str or None, got {failure!r}, {message!r}")  # pyright: ignore[reportUnreachable]
  message = None if message is None else _normalize_message(message)
  # Everything runs under the lock so concurrent first pings write `checks.json` once; nothing
  # inside may re-take it (non-reentrant).
  with _REGISTRY.lock:
    if name not in _REGISTRY.declarations:
      raise HealthCheckError(f"{name}: health check was never declared")
    # This file is the caller `get_settings` would find by walking the stack on every ping.
    settings = BaseSettings.get_settings(caller_file=__file__)
    folder = settings.log_loc_folder / "health"
    now = datetime.now(UTC)
    if _REGISTRY.written_at is None:
      # The freeze, stamped with the first ping's own `at`, so that ping is never older than the declarations.
      _REGISTRY.written_at = now
    if not _REGISTRY.declarations_written:
      checks_json = {
        "version": 1,
        "written_at": _REGISTRY.written_at.isoformat(),
        "checks": {key: decl.as_json() for key, decl in _REGISTRY.declarations.items()},
      }
      _REGISTRY.declarations_written = _write_atomic(folder / "checks.json", checks_json)
    report = _Report(at=now, failure=failure, message=message)
    if failure:
      _REGISTRY.last_failures[name] = report
    # The supervisor sees only the file as of its poll, so a failure ping a success overwrote in between
    # reaches it through `last_failure`.
    last_failure = _REGISTRY.last_failures.get(name)
    _write_atomic(
      folder / "state" / f"{name}.json",
      {
        "at": report.at.isoformat(),
        "failure": report.failure,
        "message": report.message,
        "last_failure": None if last_failure is None else {"at": last_failure.at.isoformat(), "message": last_failure.message},
      },
    )
    _REGISTRY.reports[name] = report
  if failure and not supervised_ping():
    url, autoprovision = resolve_ping_url(
      settings.alerts_healthcheck_ping_url, settings.alerts_healthcheck_pingkey, settings.heartbeat_slug
    )
    if url is not None:
      threading.Thread(
        target=ping_healthcheck,
        args=(url,),
        # devkit-container's failing judgment, so both owners of the ping send the same body.
        kwargs={"failure": True, "autoprovision": autoprovision, "body": f"{name}: {report.message or 'failure ping'}"},
        name="aeth-ext-health-fail",
        daemon=True,
      ).start()


class Tick(NamedTuple):
  """What a heartbeat must do about the health checks when no supervisor owns the ping."""

  withheld: bool
  """Some `affects_ping` check is failing (stalled or its latest ping was a failure ping), so the regular ping is skipped."""
  alerts: list[str]
  """The stall reason of each `fail_ping` check that has just stalled."""
  stop: str | None
  """The stall reason of the first `shutdown` check that has just stalled."""


def heartbeat_tick(now: datetime) -> Tick:
  """Pings the built-in `heartbeat` check if it is declared, then judges every check at `now`.

  The judgment is devkit-container's `checks::judge`, strings included. Whether each check is stalled
  is kept between calls, so a stall episode alerts and stops once. Before the freeze nothing is judged:
  without `checks.json` every check counts as healthy.
  """
  with _REGISTRY.lock:
    ping_builtin = "heartbeat" in _REGISTRY.declarations
  # Outside the lock, which `send_health_check` takes and which is not reentrant. Always a success
  # ping: it says only that the heartbeat ticked.
  if ping_builtin:
    send_health_check("heartbeat")
  withheld = False
  alerts: list[str] = []
  stop: str | None = None
  with _REGISTRY.lock:
    written_at = _REGISTRY.written_at
    if written_at is None:
      return Tick(withheld, alerts, stop)
    # In name order, as devkit-container judges, so the joined `/fail` body and the stop reason match its own.
    for name, decl in sorted(_REGISTRY.declarations.items()):
      # A ping older than the declarations (the wall clock stepped back) is ignored, as devkit-container
      # ignores its state file. Ages are differences of whole epoch seconds; a negative one is fresh.
      report = _REGISTRY.reports.get(name)
      current = report if report is not None and report.at >= written_at else None
      age = math.floor(now.timestamp()) - math.floor((current.at if current else written_at).timestamp())
      stalled = age >= decl.period + decl.grace
      if stalled and not _REGISTRY.stalled.get(name, False):
        reason = (
          f"{name}: {decl.stall_message} (no ping for {age} s)"
          if decl.stall_message
          else f"{name}: no ping for {age} s (period {decl.period} s + grace {decl.grace} s)"
        )
        if decl.fail_ping:
          alerts.append(reason)
        if decl.shutdown and stop is None:
          stop = reason
      _REGISTRY.stalled[name] = stalled
      withheld = withheld or (decl.affects_ping and (stalled or (current is not None and current.failure)))
  return Tick(withheld, alerts, stop)
