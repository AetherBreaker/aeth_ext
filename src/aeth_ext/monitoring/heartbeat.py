"""Heartbeat primitive plus its asyncio and thread schedulers, all sharing one default cadence."""

# Standard library imports
import builtins
from asyncio import to_thread, wait_for
from datetime import datetime
from functools import cache
from logging import getLogger
from threading import Thread
from typing import TYPE_CHECKING

# Third party imports
from pydantic import SecretStr

# First party imports
from aeth_ext.errors import handle_fatal_exc_async, handle_fatal_exc_sync
from aeth_ext.errors.shutdown import SHUTDOWN
from aeth_ext.monitoring.ping import ping_healthcheck
from aeth_ext.static_eval import get_caller_file, parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Coroutine
  from pathlib import Path
  from zoneinfo import ZoneInfo

logger = getLogger(__name__)

__all__ = ["HeartbeatThread", "run_heartbeat_async", "send_heartbeat", "send_heartbeat_async", "start_heartbeat_thread"]

# The standardized cadence for the scheduled options (run_heartbeat_async,
# HeartbeatThread) -- the whole point of this module is that every program
# using it heartbeats on the same interval unless it has a specific reason
# not to.
DEFAULT_HEARTBEAT_INTERVAL_SECS = 60


@cache
def _auto_slug(caller_file: str) -> str | None:
  """Looks up a ``HEARTBEAT_SLUG`` constant in *caller_file*'s own package ancestry.

  Memoised per file for the life of the process.

  Must be called with the file of whichever consumer actually asked for a
  heartbeat, never this module's own file -- ``heartbeat.py`` lives in
  ``aeth_ext.monitoring``, a sibling of every real consumer's package, so a
  search rooted here would never see a ``HEARTBEAT_SLUG`` defined by the
  caller. Each of this module's public entry points resolves this exactly
  once (at the point it can still see its true external caller) rather than
  per-ping, both for cost and because a repeated call from inside this
  module's own scheduling loops would resolve from the wrong frame.
  """
  found = parse_and_grab_constants(expected_constants={"HEARTBEAT_SLUG": "heartbeat_slug"}, caller_file=caller_file)
  return found.get("heartbeat_slug")


def _resolve_ping_url(ping_url: SecretStr | None, pingkey: SecretStr | None, slug: str | None) -> tuple[SecretStr | None, bool]:
  """Prefers a fixed, pre-created check's *ping_url*; otherwise builds a ping-key/slug URL.

  The latter needs both *pingkey* and *slug*. Returns ``(resolved_url, autoprovision)`` --
  *autoprovision* is only ``True`` for the pingkey/slug case, since that's the only one where the
  check might not exist yet (a *ping_url* is assumed pre-created). The built URL is wrapped
  straight back into a `SecretStr` -- *pingkey*'s unwrapped value is only ever pulled inline for
  this one f-string, never bound to a variable of its own.

  Contract (intentional, not an oversight): *ping_url* and *pingkey* are each either a genuinely
  configured, non-empty `SecretStr` or `None` -- an empty-but-present secret is not a supported
  way to signal "not configured" and is deliberately never checked for here, the same way this
  project never re-validates a caller-supplied `SecretStr | None` elsewhere (e.g.
  `aeth_ext.utils.batch_send_emails`'s `smtp_password`). `BaseSettings.model_config` sets
  `env_ignore_empty=True` unconditionally (`aeth_ext.settings`), so a blank env var is already
  coerced to `None` before it ever reaches this function.
  """
  # None is the sole "not configured" sentinel; callers must reject empty secrets.
  if ping_url is not None:
    return ping_url, False
  if pingkey is not None and slug:
    return SecretStr(f"https://hc-ping.com/{pingkey.get_secret_value()}/{slug}"), True
  return None, False


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

  Every public entry point resolves *slug* from its own frame before delegating
  here (see :func:`_auto_slug`), so this must never attempt that lookup itself:
  by this point the true external caller is several frames away, or -- for the
  paths that offload this to a worker thread -- not on the stack at all.

  Both steps block: the local write is ordinary file IO, and
  :func:`~aeth_ext.monitoring.ping.ping_healthcheck` performs a synchronous
  HTTP request whose timeout does **not** cover DNS resolution. Call this
  directly only from a thread that can afford to block; asyncio callers must go
  through :func:`send_heartbeat_async` (or :func:`run_heartbeat_async`), which
  offload it with :func:`asyncio.to_thread`.
  """
  if heartbeat_file is not None:
    try:
      heartbeat_file.write_text(datetime.now(tz).isoformat())
    except Exception:
      logger.exception("Failed to write heartbeat file")

  resolved_url, autoprovision = _resolve_ping_url(ping_url, pingkey, slug)
  ping_healthcheck(resolved_url, start=start, failure=failure, autoprovision=autoprovision)


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
  ever raises.

  Args:
    heartbeat_file: Local heartbeat file to timestamp. `None` skips the local file entirely
      (network-only heartbeat).
    ping_url: A pre-created healthchecks.io (or compatible) check's ping URL. Takes precedence
      over *pingkey*/*slug* if both are given.
    pingkey: A healthchecks.io ping key for auto-provisioning checks by slug. Used together with
      *slug* to build ``https://hc-ping.com/<pingkey>/<slug>`` when *ping_url* is not set.
    slug: The check's slug when using *pingkey* auto-provisioning -- typically the program's own
      name, so each program gets its own automatically created check. When omitted, looked up
      automatically from a ``HEARTBEAT_SLUG`` constant in the caller's own package ancestry (see
      ``aeth_ext.static_eval.parse_and_grab_constants``).
    start: Pass `True` to signal the start of a run rather than a plain liveness ping -- see
      ``aeth_ext.monitoring.ping.ping_healthcheck``.
    failure: Pass `True` to report a known failure instead of a liveness ping. Ignored when
      *start* is also `True`.
    tz: Time zone for the heartbeat file's timestamp.
  """
  if slug is None:
    caller_file = get_caller_file(1)
    slug = _auto_slug(caller_file) if caller_file is not None else None

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
  docstring for the parameters, including *slug* auto-detection, which resolves
  from **this** function's caller and must therefore happen here, before the
  work is handed to a thread that no longer has that caller on its stack.
  """
  if slug is None:
    caller_file = get_caller_file(1)
    slug = _auto_slug(caller_file) if caller_file is not None else None

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

  A plain (non-``async def``) function on purpose: *slug* auto-detection must
  see this function's caller, and that caller is only observable while it is
  actually being called -- once the returned coroutine is wrapped in
  :func:`~asyncio.create_task` (as callers typically do, per the example
  above) and driven by the event loop, its frame stack no longer includes
  the original caller at all.
  """
  if slug is None:
    caller_file = get_caller_file(1)
    slug = _auto_slug(caller_file) if caller_file is not None else None
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
  # _send_heartbeat rather than send_heartbeat: *slug* was already resolved by
  # run_heartbeat_async from its own caller, and each ping is offloaded with
  # to_thread so neither the file write nor the (potentially indefinite) HTTP
  # ping can stall the caller's event loop. Awaiting the offload also bounds the
  # damage of a wedged ping to a single worker thread, since the next heartbeat
  # is not dispatched until this one returns.
  async def _ping(*, start: bool) -> None:
    await to_thread(_send_heartbeat, heartbeat_file, ping_url=ping_url, pingkey=pingkey, slug=slug, start=start, failure=False, tz=tz)

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
    """Resolves *slug* from the constructing caller (see `_auto_slug`); nothing runs until `start()`."""
    super().__init__(name="aeth-ext-heartbeat", daemon=True)
    if slug is None:
      caller_file = get_caller_file(1)
      slug = _auto_slug(caller_file) if caller_file is not None else None
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

    # _send_heartbeat rather than send_heartbeat: __init__ already resolved
    # *slug* from its own caller, and re-resolving from here would search this
    # module's package ancestry instead of the consumer's. Blocking is fine --
    # this is a dedicated thread, which is the whole point of the class.
    def _ping(*, start: bool) -> None:
      _send_heartbeat(
        self._heartbeat_file,
        ping_url=self._ping_url,
        pingkey=self._pingkey,
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
    caller_file = get_caller_file(1)
    slug = _auto_slug(caller_file) if caller_file is not None else None
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
