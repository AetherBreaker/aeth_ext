# Future imports
from __future__ import annotations

# Standard library imports
import smtplib
import ssl
import sys
from email.message import EmailMessage
from logging import getLogger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # First party imports
  from sft_ext.settings import BaseSettings

main_module = sys.modules["__main__"]
RICH_CONSOLE = getattr(main_module, "RICH_CONSOLE", None)

logger = getLogger(__name__)


try:
  # Standard library imports
  import sys

  try:
    settings_module = sys.modules["environment_init_vars"]
    SETTINGS: BaseSettings = settings_module.SETTINGS
  except KeyError:
    settings_module = sys.modules["environment_settings"]
    SETTINGS: BaseSettings = settings_module.SETTINGS()
except KeyError, AttributeError:
  # First party imports
  from sft_ext.settings import BaseSettings

  SETTINGS: BaseSettings = BaseSettings()  # type: ignore


ALERTS_EMAIL = SETTINGS.alerts_email
ALERTS_EMAIL_PWD = SETTINGS.alerts_email_pwd
ALERTS_RECIPIENTS = SETTINGS.alerts_recipients


def send_alert_email(subject: str, content: str) -> None:
  if not ALERTS_RECIPIENTS:
    logger.warning("Skipping alert email because no recipients are configured.")
    return

  msg = EmailMessage()
  msg.set_content("View attachment")
  msg["Subject"] = subject
  msg["From"] = ALERTS_EMAIL
  msg["To"] = ", ".join([str(recipient) for recipient in ALERTS_RECIPIENTS])
  context = ssl.create_default_context()

  msg.add_attachment(
    "\ufeff" + content,  # UTF-8 BOM so Windows apps detect encoding correctly
    subtype="plain",
    filename="alert.txt",
    charset="utf-8",
  )

  try:
    with smtplib.SMTP(SETTINGS.alerts_smtp_server, SETTINGS.alerts_smtp_port) as server:
      server.ehlo()
      server.starttls(context=context)
      server.ehlo()
      server.login(ALERTS_EMAIL, ALERTS_EMAIL_PWD)
      server.send_message(msg)
    logger.debug("Alert email sent successfully.")
  except Exception:
    logger.error("Failed to send alert email.", exc_info=True)
