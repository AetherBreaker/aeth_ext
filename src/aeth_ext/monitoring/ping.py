# Standard library imports
from http.client import HTTPException
from logging import getLogger
from urllib.error import URLError
from urllib.request import urlopen

# Third party imports
from pydantic import SecretStr

logger = getLogger(__name__)

__all__ = ["ping_failure", "ping_healthcheck", "ping_start", "ping_success"]

_REQUEST_TIMEOUT_SECS = 10


def ping_healthcheck(url: SecretStr | None, *, failure: bool = False, start: bool = False, autoprovision: bool = False) -> None:
  """Best-effort liveness ping to an external dead-man's-switch service (e.g. healthchecks.io).

  Call this from the same periodic heartbeat that already writes a local
  heartbeat file/timestamp. The external service alerts when pings stop
  arriving, which -- unlike a locally-written heartbeat file -- catches a
  hung process even when nothing is watching that file.

  A no-op when *url* is `None`/empty, so callers don't need to guard every call site on whether a
  ping URL is configured. Never raises: a monitoring ping must not be able to bring down the
  process it's reporting the liveness of.

  *url* is a `SecretStr` because it may embed a bearer-token-like secret in its path (e.g. a
  healthchecks.io ping-key/slug URL). Its unwrapped value is never bound to a variable -- only
  ever pulled inline at the one place that needs it (the `urlopen` call) -- and logging always
  passes the `SecretStr` object itself so it prints masked.

  Args:
    url: The service's ping URL. `None`/empty is a no-op.
    failure: Pass `True` to report a known failure instead of a liveness ping, for services (e.g.
      healthchecks.io's ``/fail`` suffix) that support signaling "still running, but broken"
      distinctly from silence. Ignored when *start* is also `True`.
    start: Pass `True` to signal the start of a run (e.g. healthchecks.io's ``/start`` suffix),
      rather than a plain liveness or failure ping. Takes precedence over *failure*.
    autoprovision: Pass `True` when *url* is a healthchecks.io ping-key/slug URL that hasn't
      necessarily been created yet, to append ``?create=1`` -- healthchecks.io only auto-creates
      the check on first ping when this is explicitly requested; otherwise pinging a slug that
      doesn't exist yet just 404s. Must come after any ``/start``/``/fail`` suffix, since a query
      string always follows the full path.
  """
  if url is None or not url.get_secret_value():
    return

  suffix = "/start" if start else "/fail" if failure else ""
  query = "?create=1" if autoprovision else ""
  ping_url = SecretStr(f"{url.get_secret_value()}{suffix}{query}")

  logger.debug("Sending healthcheck ping to %s", ping_url)

  try:
    with urlopen(ping_url.get_secret_value(), timeout=_REQUEST_TIMEOUT_SECS):
      pass
  except (URLError, OSError, HTTPException) as e:
    # Not just URLError: urllib only wraps the *send* in URLError -- a timeout/reset/SSL error
    # while *reading* the response escapes getresponse() as a raw OSError (and a malformed
    # response as an HTTPException). One slow hc-ping.com response once took down every service
    # at the same moment because this caught URLError alone.
    logger.warning("Failed to ping healthcheck at %s", ping_url, exc_info=e)


def ping_success(url: SecretStr | None) -> None:
  """Send a plain success/liveness ping. Equivalent to ``ping_healthcheck(url)``."""
  ping_healthcheck(url)


def ping_start(url: SecretStr | None) -> None:
  """Signal the start of a run. Equivalent to ``ping_healthcheck(url, start=True)``."""
  ping_healthcheck(url, start=True)


def ping_failure(url: SecretStr | None) -> None:
  """Report a known failure. Equivalent to ``ping_healthcheck(url, failure=True)``."""
  ping_healthcheck(url, failure=True)
