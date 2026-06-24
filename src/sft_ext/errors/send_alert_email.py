# Standard library imports
from logging import getLogger

# First party imports
from sft_ext.settings import BaseSettings
from sft_ext.types import EmailMessageParts
from sft_ext.utils import batch_send_emails, prepare_email_message

logger = getLogger(__name__)

SETTINGS = BaseSettings.get_settings()


def send_alert_email(subject: str, content: str) -> None:
  if not SETTINGS.alerts_recipients:
    logger.warning("Skipping alert email because no recipients are configured.")
    return

  msg = prepare_email_message(
    EmailMessageParts(
      subject=subject,
      body="View attachment",
      from_addr=SETTINGS.alerts_email,
      to_addrs=", ".join([str(recipient) for recipient in SETTINGS.alerts_recipients]),
    )
  )

  msg.add_attachment(
    "\ufeff" + content,  # UTF-8 BOM so Windows apps detect encoding correctly
    subtype="plain",
    filename="alert.txt",
    charset="utf-8",
  )

  try:
    batch_send_emails(email_messages=[msg])
    logger.debug("Alert email sent successfully.")
  except Exception:
    logger.error("Failed to send alert email.", exc_info=True)
