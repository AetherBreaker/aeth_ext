# Standard library imports
from html import escape
from logging import getLogger
from typing import TYPE_CHECKING, cast

# First party imports
from aeth_ext.settings import BaseSettings
from aeth_ext.types import EmailMessageParts
from aeth_ext.utils import batch_send_emails, prepare_email_message

if TYPE_CHECKING:
  # Standard library imports
  from email.message import EmailMessage

logger = getLogger(__name__)

__all__ = ["send_alert_email"]

SETTINGS = BaseSettings.get_settings()

_IMAGE_CID = "traceback-image"


def _add_inline_image(msg: EmailMessage, subject: str, image: bytes) -> None:
  """Add an HTML alternative body with *image* embedded inline via a ``cid:`` reference.

  Displays directly in the email body in HTML-capable clients, rather than
  appearing as a separate download -- plain-text-only clients still see the
  existing plain-text body untouched (this only adds an alternative, it
  doesn't replace it).
  """
  html_body = (
    f"<html><body><p>{escape(subject)}</p>"
    f'<img src="cid:{_IMAGE_CID}" alt="Traceback" style="max-width:100%;">'
    "<p>Full traceback attached as alert.txt for text search.</p></body></html>"
  )
  msg.add_alternative(html_body, subtype="html")
  html_part = cast("EmailMessage", msg.get_payload()[-1])  # pyright: ignore[reportArgumentType]
  html_part.add_related(image, maintype="image", subtype="png", cid=f"<{_IMAGE_CID}>")


def send_alert_email(subject: str, content: str, *, image: bytes | None = None) -> None:
  try:
    if not SETTINGS.alerts_recipients:
      logger.warning("Skipping alert email because no recipients are configured.")
      return

    assert isinstance(SETTINGS.alerts_email, str), "SETTINGS.alerts_email must be a string"
    msg = prepare_email_message(
      EmailMessageParts(
        subject=subject,
        body="View attachment",
        from_addr=("SFT ALERTS", None, None, SETTINGS.alerts_email),
        to_addrs=", ".join([str(recipient) for recipient in SETTINGS.alerts_recipients]),
      )
    )

    if image is not None:
      _add_inline_image(msg, subject, image)

    # Always attached, regardless of the image above: the only complete,
    # ctrl+F-searchable record of the traceback -- the image is a syntax
    # highlighted convenience on top, not a replacement. Added last so it lands
    # as a sibling of the alternative body, not nested inside it.
    msg.add_attachment(
      "\ufeff" + content,  # UTF-8 BOM so Windows apps detect encoding correctly
      subtype="plain",
      filename="alert.txt",
      charset="utf-8",
    )

    batch_send_emails(email_messages=[msg])
    logger.debug("Alert email sent successfully.")
  except Exception:
    logger.exception("Failed to send alert email.")
