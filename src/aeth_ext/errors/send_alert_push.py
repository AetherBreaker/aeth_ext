# Standard library imports
from base64 import b64encode
from logging import getLogger
from typing import TYPE_CHECKING, NotRequired, TypedDict
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# First party imports
from aeth_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Third party imports
  from pydantic import SecretStr

logger = getLogger(__name__)

__all__ = ["send_alert_push"]

SETTINGS = BaseSettings.get_settings()

_PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
_REQUEST_TIMEOUT_SECS = 10

# Pushover's "emergency" priority: repeats the notification and requires
# acknowledgment instead of just appearing once, which is the point of a
# second channel -- a missed push and a missed email should not both be
# silently ignorable.
_EMERGENCY_RETRY_SECS = 60
_EMERGENCY_EXPIRE_SECS = 3600

# Pushover rejects messages over 1024 UTF-8 characters outright, rather than
# truncating them itself -- a fatal exception with a long traceback would
# otherwise silently fail to alert at all, the exact failure mode this
# channel exists to catch. The attached image (when rendering succeeded)
# carries the full syntax-highlighted traceback anyway, so this is just a
# preview.
_MESSAGE_MAX_LEN = 1024
_TRUNCATION_SUFFIX = "\n... (truncated -- see attached image/email for the full traceback)"


def _truncate_message(message: str) -> str:
  if len(message) <= _MESSAGE_MAX_LEN:
    return message
  return message[: _MESSAGE_MAX_LEN - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


class PushPayload(TypedDict):
  token: SecretStr
  user: SecretStr
  title: str
  message: str
  priority: int
  retry: NotRequired[int | None]
  expire: NotRequired[int | None]
  attachment_base64: NotRequired[str | None]
  attachment_type: NotRequired[str | None]


def send_alert_push(title: str, message: str, *, priority: int = 0, image: bytes | None = None) -> None:
  try:
    if SETTINGS.alerts_pushover_token is None or SETTINGS.alerts_pushover_user_key is None:
      # None is the sole sentinel for an unconfigured credential; empty secrets are caller errors.
      logger.warning("Skipping push alert because Pushover is not configured.")
      return

    payload: PushPayload = {
      "token": SETTINGS.alerts_pushover_token,
      "user": SETTINGS.alerts_pushover_user_key,
      "title": title,
      "message": _truncate_message(message),
      "priority": priority,
    }
    if priority == 2:  # noqa: PLR2004 - Pushover's own priority scale, not a project constant
      payload["retry"] = _EMERGENCY_RETRY_SECS
      payload["expire"] = _EMERGENCY_EXPIRE_SECS

    if image is not None:
      # attachment_base64 (rather than a multipart/form-data body) keeps this
      # on the same simple x-www-form-urlencoded POST as everything else --
      # ~35% more request bytes than a raw multipart upload, but at our image
      # sizes (well under Pushover's 5MB attachment cap) that's irrelevant.
      payload["attachment_base64"] = b64encode(image).decode("ascii")
      payload["attachment_type"] = "image/png"

    request = Request(
      _PUSHOVER_API_URL,
      data=urlencode({**payload, "token": payload["token"].get_secret_value(), "user": payload["user"].get_secret_value()}).encode(
        "utf-8"
      ),
      headers={"Content-Type": "application/x-www-form-urlencoded"},
      method="POST",
    )

    with urlopen(request, timeout=_REQUEST_TIMEOUT_SECS) as response:
      if response.status != 200:  # noqa: PLR2004 - HTTP 200, not a project constant
        logger.error("Pushover alert failed with status %d", response.status)
      else:
        logger.debug("Push alert sent successfully.")
  except Exception:
    logger.exception("Failed to send push alert.")
