# Standard library imports
from logging import getLogger
from urllib.error import URLError
from urllib.request import urlopen

logger = getLogger(__name__)

__all__ = ["ping_failure", "ping_healthcheck", "ping_start", "ping_success"]

_REQUEST_TIMEOUT_SECS = 10


def ping_healthcheck(url: str | None, *, failure: bool = False, start: bool = False, autoprovision: bool = False) -> None:
  """Best-effort liveness ping to an external dead-man's-switch service (e.g. healthchecks.io).

  Call this from the same periodic heartbeat that already writes a local
  heartbeat file/timestamp. The external service alerts when pings stop
  arriving, which -- unlike a locally-written heartbeat file -- catches a
  hung process even when nothing is watching that file.

  A no-op when *url* is falsy, so callers don't need to guard every call site
  on whether a ping URL is configured. Never raises: a monitoring ping must
  not be able to bring down the process it's reporting the liveness of.

  :param url: The service's ping URL. ``None``/empty is a no-op.
  :param failure: Pass ``True`` to report a known failure instead of a
      liveness ping, for services (e.g. healthchecks.io's ``/fail`` suffix)
      that support signaling "still running, but broken" distinctly from
      silence. Ignored when *start* is also ``True``.
  :param start: Pass ``True`` to signal the start of a run (e.g.
      healthchecks.io's ``/start`` suffix), rather than a plain liveness or
      failure ping. Takes precedence over *failure*.
  :param autoprovision: Pass ``True`` when *url* is a healthchecks.io
      ping-key/slug URL that hasn't necessarily been created yet, to append
      ``?create=1`` -- healthchecks.io only auto-creates the check on first
      ping when this is explicitly requested; otherwise pinging a slug that
      doesn't exist yet just 404s. Must come after any ``/start``/``/fail``
      suffix, since a query string always follows the full path.
  """
  if not url:
    return

  if start:
    ping_url = f"{url}/start"
  elif failure:
    ping_url = f"{url}/fail"
  else:
    ping_url = url

  if autoprovision:
    ping_url = f"{ping_url}?create=1"

  try:
    with urlopen(ping_url, timeout=_REQUEST_TIMEOUT_SECS):
      pass
  except URLError as e:
    logger.warning("Failed to ping healthcheck at %s", ping_url, exc_info=e)


def ping_success(url: str | None) -> None:
  """Send a plain success/liveness ping. Equivalent to ``ping_healthcheck(url)``."""
  ping_healthcheck(url)


def ping_start(url: str | None) -> None:
  """Signal the start of a run. Equivalent to ``ping_healthcheck(url, start=True)``."""
  ping_healthcheck(url, start=True)


def ping_failure(url: str | None) -> None:
  """Report a known failure. Equivalent to ``ping_healthcheck(url, failure=True)``."""
  ping_healthcheck(url, failure=True)
