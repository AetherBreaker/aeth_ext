"""Heartbeat primitive plus its asyncio and thread schedulers, all sharing one default cadence."""

# Standard library imports
import builtins
from asyncio import to_thread, wait_for
from datetime import UTC, datetime
from logging import getLogger
from threading import Thread
from typing import TYPE_CHECKING

# First party imports
from aeth_ext.errors import handle_fatal_exc_async, handle_fatal_exc_sync
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind, run_shutdown
from aeth_ext.monitoring.health import adopt_heartbeat_interval, heartbeat_tick, record_ping_destination
from aeth_ext.monitoring.ping import (
  _SHUTDOWN_PING_WAIT_SECS,  # pyright: ignore[reportPrivateUsage]
  ping_healthcheck,
  resolve_ping_url,
  supervised_ping,
)
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Coroutine
  from pathlib import Path
  from zoneinfo import ZoneInfo

  # Third party imports
  from pydantic import SecretStr

logger = getLogger(__name__)

__all__ = ["HeartbeatThread", "run_heartbeat_async", "send_heartbeat", "send_heartbeat_async", "start_heartbeat_thread"]

# The standardized cadence for the scheduled options (run_heartbeat_async,
# HeartbeatThread) -- the whole point of this module is that every program
# using it heartbeats on the same interval unless it has a specific reason
# not to.
DEFAULT_HEARTBEAT_INTERVAL_SECS = 60


def _send_heartbeat(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None,
  pingkey: SecretStr | None,
  slug: str | None,
  start: bool,
  failure: bool,
  tz: ZoneInfo | None,
) -> None:
  """Blocking heartbeat primitive, with *slug* already resolved by the caller.

  Both steps block: the local write is ordinary file IO, and
  :func:`~aeth_ext.monitoring.ping.ping_healthcheck` performs a synchronous
  HTTP request whose timeout does **not** cover DNS resolution. Call this
  directly only from a thread that can afford to block; asyncio callers must go
  through :func:`send_heartbeat_async` (or :func:`run_heartbeat_async`), which
  offload it with :func:`asyncio.to_thread`.

  Unsupervised, it applies the health-check rules (`heartbeat_tick`) to the ping: skipped while
  withheld unless *failure*, replaced by one `/fail` carrying the new stalls' reasons, and a new
  `shutdown` stall stops the app when `__debug__` is off, skipping the ping and waiting on that `/fail`
  for at most `_SHUTDOWN_PING_WAIT_SECS`. Supervised, the supervisor applies them.
  """
  if heartbeat_file is not None:
    try:
      heartbeat_file.write_text(datetime.now(tz).isoformat())
    except Exception:
      logger.exception("Failed to write heartbeat file")

  # Ticks even when supervised: it pings the built-in check and writes the files the supervisor judges.
  tick = heartbeat_tick(datetime.now(UTC))
  # Asked here rather than inferred from the URLs: a direct `send_heartbeat` passes them even when supervised.
  supervised = supervised_ping()
  resolved_url, autoprovision = resolve_ping_url(ping_url, pingkey, slug)
  # Before any ping: a supervised scheduler nulls its URLs, which must not displace the destination.
  if resolved_url is not None and not supervised:
    record_ping_destination(resolved_url, autoprovision)
  if tick.stop is not None and not supervised and not __debug__:
    logger.error("Shutting down for a stalled health check: %s", tick.stop)
    # No regular ping, as the app is going down. The alert gets its own thread and a bounded wait, so a
    # wedged resolver, which `urlopen`'s timeout doesn't cover, can't hold the shutdown back.
    if tick.alerts:
      alert = Thread(
        target=ping_healthcheck,
        args=(resolved_url,),
        kwargs={"failure": True, "autoprovision": autoprovision, "body": "\n".join(tick.alerts)},
        name="aeth-ext-shutdown-alert",
        daemon=True,
      )
      alert.start()
      alert.join(_SHUTDOWN_PING_WAIT_SECS)
    run_shutdown(ShutdownKind.GRACEFUL)
    return
  if tick.alerts and not supervised:
    # In place of the regular ping, which right after would turn the check straight back up; the
    # supervisor likewise sends one `/fail` per poll.
    ping_healthcheck(resolved_url, failure=True, autoprovision=autoprovision, body="\n".join(tick.alerts))
  elif supervised or failure or not tick.withheld:
    ping_healthcheck(resolved_url, start=start, failure=failure, autoprovision=autoprovision)
  if tick.stop is not None and not supervised:
    # Reached only with `__debug__` on: a paused debugger stalls every check, so a debug run is never stopped for it.
    logger.warning("Not shutting down a debug run for a stalled health check: %s", tick.stop)


def send_heartbeat(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None = None,
  pingkey: SecretStr | None = None,
  slug: str | None = None,
  start: bool = False,
  failure: bool = False,
  tz: ZoneInfo | None = None,
) -> None:
  """Write *heartbeat_file* (if given) and ping an external dead-man's-switch (if configured).

  Warning:
    This blocks the calling thread -- on file IO and, more importantly, on a synchronous HTTP
    request that can outlast its own timeout (``urlopen`` applies the timeout to socket
    operations only, never to ``getaddrinfo``, so a wedged resolver blocks indefinitely).
    **Never call this from an asyncio event loop**; use `send_heartbeat_async` there instead.

  This is the primitive that `run_heartbeat_async` and `HeartbeatThread` are both built on, but
  it's also meant to be called directly from a consumer's own scheduler (cron, APScheduler, a
  bespoke loop) when neither of those fits. The local write and the network ping are independent,
  best-effort signals -- a failure in one never prevents or is affected by the other, and neither
  ever raises. Declared health checks can withhold the ping or turn it into a `/fail`
  (`aeth_ext.monitoring.health`).

  Args:
    heartbeat_file: Local heartbeat file to timestamp. `None` skips the local file entirely
      (network-only heartbeat).
    ping_url: A pre-created healthchecks.io (or compatible) check's ping URL. Takes precedence
      over *pingkey*/*slug* if both are given.
    pingkey: A healthchecks.io ping key for auto-provisioning checks by slug. Used together with
      *slug* to build ``https://hc-ping.com/<pingkey>/<slug>`` when *ping_url* is not set.
    slug: The check's slug when using *pingkey* auto-provisioning -- typically the program's own
      name, so each program gets its own automatically created check. When omitted, taken from
      the ``heartbeat_slug`` setting (``HEARTBEAT_SLUG``).
    start: Pass `True` to signal the start of a run rather than a plain liveness ping -- see
      ``aeth_ext.monitoring.ping.ping_healthcheck``.
    failure: Pass `True` to report a known failure instead of a liveness ping. Ignored when
      *start* is also `True`.
    tz: Time zone for the heartbeat file's timestamp.
  """
  if slug is None:
    slug = BaseSettings.get_settings().heartbeat_slug

  _send_heartbeat(heartbeat_file, ping_url=ping_url, pingkey=pingkey, slug=slug, start=start, failure=failure, tz=tz)


async def send_heartbeat_async(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None = None,
  pingkey: SecretStr | None = None,
  slug: str | None = None,
  start: bool = False,
  failure: bool = False,
  tz: ZoneInfo | None = None,
) -> None:
  """Asyncio-safe :func:`send_heartbeat`: identical behaviour, off the event loop.

  Both halves of a heartbeat block -- the local file write, and a synchronous
  ``urlopen`` whose timeout does not cover DNS resolution -- so running them
  inline on an event loop stalls every task on it, and a wedged resolver stalls
  them indefinitely. This offloads the whole thing with
  :func:`asyncio.to_thread` and awaits it, which also means a hung ping can
  never tie up more than the one worker thread: the next heartbeat is not
  dispatched until this one returns.

  Await it for the same one-shot use :func:`send_heartbeat` covers (e.g. a
  "start" ping during an async boot sequence). For the recurring case use
  :func:`run_heartbeat_async`, which already offloads each ping this way.

  Accepts and means exactly what :func:`send_heartbeat` does -- see its
  docstring for the parameters.
  """
  if slug is None:
    slug = BaseSettings.get_settings().heartbeat_slug

  await to_thread(_send_heartbeat, heartbeat_file, ping_url=ping_url, pingkey=pingkey, slug=slug, start=start, failure=failure, tz=tz)


def run_heartbeat_async(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None = None,
  pingkey: SecretStr | None = None,
  slug: str | None = None,
  interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECS,
  send_start: bool = True,
  tz: ZoneInfo | None = None,
) -> Coroutine[None, None, None]:
  """Coroutine ready to be scheduled as a background asyncio task.

  Sends an initial heartbeat immediately -- a "start" ping by default, or a
  plain one if *send_start* is ``False`` -- then a plain heartbeat every
  *interval* seconds, until :data:`~aeth_ext.errors.SHUTDOWN` is requested -- at
  which point it returns on its own within one wait cycle, rather than
  needing to be cancelled from outside::

      heartbeat_task = create_task(run_heartbeat_async(heartbeat_file, pingkey=..., slug="my-app"))
      ...
      await SHUTDOWN
      # no need to cancel heartbeat_task -- it has already stopped itself
  """
  if slug is None:
    slug = BaseSettings.get_settings().heartbeat_slug
  # Only a custom interval, so a default-interval scheduler leaves an app's own period for the built-in check alone.
  if interval != DEFAULT_HEARTBEAT_INTERVAL_SECS:
    adopt_heartbeat_interval(interval)
  return _run_heartbeat_async(
    heartbeat_file, ping_url=ping_url, pingkey=pingkey, slug=slug, interval=interval, send_start=send_start, tz=tz
  )


@handle_fatal_exc_async
async def _run_heartbeat_async(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None,
  pingkey: SecretStr | None,
  slug: str | None,
  interval: float,
  send_start: bool,
  tz: ZoneInfo | None,
) -> None:
  # Each ping is offloaded with to_thread so neither the file write nor the
  # (potentially indefinite) HTTP ping can stall the caller's event loop.
  # Awaiting the offload also bounds the damage of a wedged ping to a single
  # worker thread, since the next heartbeat is not dispatched until this one
  # returns.
  async def _ping(*, start: bool) -> None:
    await to_thread(
      _send_heartbeat,
      heartbeat_file,
      ping_url=None if supervised_ping() else ping_url,
      pingkey=None if supervised_ping() else pingkey,
      slug=slug,
      start=start,
      failure=False,
      tz=tz,
    )

  await _ping(start=send_start)

  while not SHUTDOWN.is_set():
    try:
      await wait_for(SHUTDOWN, timeout=interval)
    except builtins.TimeoutError:
      await _ping(start=False)


class HeartbeatThread(Thread):
  """Self-contained heartbeat thread for programs not using asyncio.

  Sends a "start" heartbeat immediately upon `start()`, then a plain
  heartbeat every *interval* seconds. Watches
  :data:`~aeth_ext.errors.SHUTDOWN` via its blocking ``wait()`` -- doubling
  as both the sleep and the stop signal -- so the loop exits within one
  ``wait()`` call of a shutdown being requested and the thread becomes joinable
  almost immediately, with no polling loop or explicit cancellation needed.

  Prefer :func:`start_heartbeat_thread` for the one-line create-and-start
  entry point; construct this directly only if you need to delay `start()`.
  """

  def __init__(
    self,
    heartbeat_file: Path | None,
    *,
    ping_url: SecretStr | None = None,
    pingkey: SecretStr | None = None,
    slug: str | None = None,
    interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECS,
    send_start: bool = True,
    tz: ZoneInfo | None = None,
  ) -> None:
    """Falls back to the ``heartbeat_slug`` setting when *slug* is omitted; nothing runs until `start()`."""
    super().__init__(name="aeth-ext-heartbeat", daemon=True)
    # As in `run_heartbeat_async`.
    if interval != DEFAULT_HEARTBEAT_INTERVAL_SECS:
      adopt_heartbeat_interval(interval)
    if slug is None:
      slug = BaseSettings.get_settings().heartbeat_slug
    self._heartbeat_file = heartbeat_file
    self._ping_url = ping_url
    self._pingkey = pingkey
    self._slug = slug
    self._interval = interval
    self._send_start = send_start
    self._tz = tz

  @handle_fatal_exc_sync
  def run(self) -> None:
    """Sends the initial heartbeat, then one per *interval* until `SHUTDOWN` is set."""

    # Blocking is fine -- this is a dedicated thread, which is the whole point of the class.
    def _ping(*, start: bool) -> None:
      _send_heartbeat(
        self._heartbeat_file,
        ping_url=None if supervised_ping() else self._ping_url,
        pingkey=None if supervised_ping() else self._pingkey,
        slug=self._slug,
        start=start,
        failure=False,
        tz=self._tz,
      )

    _ping(start=self._send_start)

    while not SHUTDOWN.is_set():
      woken_by_shutdown = SHUTDOWN.wait(timeout=self._interval)
      if not woken_by_shutdown:
        _ping(start=False)


def start_heartbeat_thread(
  heartbeat_file: Path | None,
  *,
  ping_url: SecretStr | None = None,
  pingkey: SecretStr | None = None,
  slug: str | None = None,
  interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECS,
  send_start: bool = True,
  tz: ZoneInfo | None = None,
) -> HeartbeatThread:
  """Create, start, and return a :class:`HeartbeatThread` -- the one-line entry point for non-asyncio programs."""
  if slug is None:
    slug = BaseSettings.get_settings().heartbeat_slug
  thread = HeartbeatThread(
    heartbeat_file,
    ping_url=ping_url,
    pingkey=pingkey,
    slug=slug,
    interval=interval,
    send_start=send_start,
    tz=tz,
  )
  thread.start()
  return thread
