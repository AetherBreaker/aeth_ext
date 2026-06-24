# Standard library imports
from collections.abc import Sequence
from datetime import datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from logging import getLogger
from mimetypes import guess_type
from pathlib import Path
from smtplib import SMTP
from ssl import create_default_context
from sys import modules
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

# Third party imports
from dateutil.relativedelta import SA, relativedelta
from dateutil.utils import today as _today

# First party imports
from sft_ext.const_parsing import parse_and_grab_constants
from sft_ext.settings import BaseSettings

if TYPE_CHECKING:
  # Standard library imports
  from zoneinfo import ZoneInfo

  type IntOrInf = int | float

logger = getLogger(__name__)


expected_consts = parse_and_grab_constants(
  Path(cast("str", modules["__main__"].__file__)), {"SHIFT": "shift"}, {"timedelta": timedelta}
)

shift = expected_consts.get("shift", timedelta())

SETTINGS = BaseSettings.get_settings()


def today(tzinfo: ZoneInfo | None = None) -> datetime:
  """
  Returns a :py:class:`datetime` representing the current day at midnight

  :param tzinfo:
      The time zone to attach (also used to determine the current day).

  :return:
      A :py:class:`datetime.datetime` object representing the current day
      at midnight.
  """

  result = _today(tzinfo=tzinfo)

  result += shift

  return result


def get_now(tzinfo: ZoneInfo | None = None) -> datetime:
  """
  Returns a :py:class:`datetime` representing the current date and time

  :param tzinfo:
      The time zone to attach (also used to determine the current date and time).

  :return:
      A :py:class:`datetime.datetime` object representing the current date and time.
  """

  result = datetime.now(tz=tzinfo)

  result += shift

  return result


def get_last_sat(dt: datetime | None = None, tzinfo: ZoneInfo | None = None) -> datetime:
  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(-1))


def get_next_sat(dt: datetime | None = None, tzinfo: ZoneInfo | None = None) -> datetime:
  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(+1))


AddressLike = str | Address | tuple[str, str | None, str | None, str | None]


class EmailMessageParts(TypedDict):
  subject: str
  body: str
  from_addr: AddressLike
  to_addrs: Sequence[AddressLike] | AddressLike
  cc_addrs: NotRequired[Sequence[AddressLike] | AddressLike]
  bcc_addrs: NotRequired[Sequence[AddressLike] | AddressLike]
  attachments: NotRequired[Sequence[Path] | Path]


def handle_addrlike(addr: AddressLike) -> str | Address:
  match addr:
    case str() as addr_str:
      return addr_str
    case Address() as addr_obj:
      return addr_obj
    case (display_name, username, domain, addr_spec):
      return Address(display_name=display_name, username=username, domain=domain, addr_spec=addr_spec)
    case _:
      raise TypeError(f"Invalid type for address: {type(addr)}")  # pyright: ignore[reportUnreachable]


def handle_addrlike_sequence(addrs: Sequence[AddressLike] | AddressLike) -> tuple[str | Address, ...] | str | Address:
  match addrs:
    #  determine whether addrs is a true sequence of addresses or a tuple containing 4 Address args
    case (str(), str() | None, str() | None, str() | None):
      return handle_addrlike(cast("AddressLike", addrs))
    case str() | Address() as single_addr:
      return handle_addrlike(single_addr)
    case Sequence() as addrs_seq:
      return tuple(handle_addrlike(addr) for addr in addrs_seq)
    case _:
      raise TypeError(f"Invalid type for addresses: {type(addrs)}")  # pyright: ignore[reportUnreachable]


def handle_attachment(attachment: Path) -> tuple[bytes, dict[str, str]]:
  """
  Handles an attachment by reading its content and determining its MIME type.

  :param attachment:
      A :py:class:`pathlib.Path` object representing the attachment file.

  :return:
      A tuple containing the attachment's content as bytes, its maintype, subtype, and filename.
  """

  ctype, encoding = guess_type(attachment.name)
  if ctype is None or encoding is not None:
    # Fallback to a generic binary stream if type is unknown
    ctype = "application/octet-stream"

  maintype, subtype = ctype.split("/", 1)

  return attachment.read_bytes(), {
    "maintype": maintype,
    "subtype": subtype,
    "filename": attachment.name,
  }


def prepare_email_message(parts: EmailMessageParts) -> EmailMessage:
  """
  Prepares an email message based on the provided parts.

  :param parts:
      A dictionary containing the components of the email message.

  :return:
      An instance of :py:class:`email.message.EmailMessage` representing the prepared email.
  """

  msg = EmailMessage()
  msg["Subject"] = parts["subject"]
  msg.set_content(parts["body"])

  # Handle the 'From' address based on its type
  msg["From"] = handle_addrlike(parts["from_addr"])

  # Handle the 'To' addresses based on their types
  msg["To"] = handle_addrlike_sequence(parts["to_addrs"])

  # Handle the 'Cc' addresses if provided
  if "cc_addrs" in parts:
    msg["Cc"] = handle_addrlike_sequence(parts["cc_addrs"])

  # Handle the 'Bcc' addresses if provided
  if "bcc_addrs" in parts:
    msg["Bcc"] = handle_addrlike_sequence(parts["bcc_addrs"])

  # Handle attachments if provided
  if "attachments" in parts:
    match parts["attachments"]:
      case Path() as single_attachment:
        attachment_content, attachment_info = handle_attachment(single_attachment)
        msg.add_attachment(attachment_content, **attachment_info)  # pyright: ignore[reportArgumentType]
      case Sequence() as attachments_seq:
        for attachment in attachments_seq:
          attachment_content, attachment_info = handle_attachment(attachment)

          msg.add_attachment(attachment_content, **attachment_info)  # pyright: ignore[reportArgumentType]
      case _:
        raise TypeError(f"Invalid type for attachments: {type(parts['attachments'])}")  # pyright: ignore[reportUnreachable]

  return msg


def batch_send_emails(
  email_messages: Sequence[EmailMessage],
  smtp_server: str | None = None,
  smtp_port: int | None = None,
  smtp_user: str | None = None,
  smtp_password: str | None = None,
) -> None:
  """
  Sends a batch of email messages.

  :param email_messages:
      A sequence of :py:class:`email.message.EmailMessage` instances to be sent.
  """

  # Standard library imports

  # Use default SMTP server settings if not provided
  smtp_server = smtp_server or SETTINGS.alerts_smtp_server
  smtp_port = smtp_port or SETTINGS.alerts_smtp_port
  smtp_user = smtp_user or SETTINGS.alerts_email
  smtp_password = smtp_password or SETTINGS.alerts_email_pwd

  ctx = create_default_context()

  with SMTP(smtp_server, smtp_port) as server:
    server.ehlo()
    server.starttls(context=ctx)
    server.ehlo()
    server.login(smtp_user, smtp_password)

    for msg in email_messages:
      server.send_message(msg)
      logger.info(f"Email sent with subject '{msg['Subject']}' to {msg['To']}")
