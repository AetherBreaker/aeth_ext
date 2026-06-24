# Standard library imports
from logging import getLogger

# Third party imports
from rich import get_console

# First party imports
from sft_ext.settings import BaseSettings
from sft_ext.types import EmailMessageParts
from sft_ext.utils import batch_send_emails, prepare_email_message

SETTINGS = BaseSettings.get_settings()

RICH_CONSOLE = get_console()

logger = getLogger(__name__)


ALERTS_EMAIL = SETTINGS.alerts_email
ALERTS_EMAIL_PWD = SETTINGS.alerts_email_pwd
ALERTS_RECIPIENTS = SETTINGS.alerts_recipients


def send_alert_email(subject: str, content: str) -> None:
  if not ALERTS_RECIPIENTS:
    logger.warning("Skipping alert email because no recipients are configured.")
    return

  msg = prepare_email_message(
    EmailMessageParts(
      subject=subject,
      body="View attachment",
      from_addr=ALERTS_EMAIL,
      to_addrs=", ".join([str(recipient) for recipient in ALERTS_RECIPIENTS]),
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
