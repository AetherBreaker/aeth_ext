# Standard library imports
from datetime import UTC, datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from typing import TYPE_CHECKING, ClassVar, Self
from zoneinfo import ZoneInfo

# Third party imports
import pytest
from pydantic import SecretStr

# First party imports
from aeth_ext import utils

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator
  from pathlib import Path

  # First party imports
  from aeth_ext.types import EmailMessageParts

_SATURDAY = 5
_TEST_SMTP_PORT = 2525


class TestTodayAndGetNow:
  def test_today_applies_the_configured_shift(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "shift", timedelta(days=3))

    result = utils.today()
    unshifted = utils._today()  # pyright: ignore[reportPrivateUsage]

    assert result == unshifted + timedelta(days=3)

  def test_get_now_applies_the_configured_shift(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "shift", timedelta(hours=-2))

    before = datetime.now(tz=UTC)
    result = utils.get_now(tzinfo=ZoneInfo("UTC"))

    assert result <= before + timedelta(hours=-2) + timedelta(seconds=5)
    assert result >= before + timedelta(hours=-2) - timedelta(seconds=5)

  def test_get_now_with_zero_shift_keeps_the_requested_tzinfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "shift", timedelta())

    tz = ZoneInfo("UTC")
    result = utils.get_now(tzinfo=tz)

    assert result.tzinfo == tz


class TestLastAndNextSaturday:
  def test_get_last_sat_is_never_after_the_reference_date(self) -> None:
    ref = datetime(2026, 7, 31, tzinfo=UTC)  # Friday

    result = utils.get_last_sat(dt=ref)

    assert result.weekday() == _SATURDAY
    assert result <= ref

  def test_get_next_sat_is_never_before_the_reference_date(self) -> None:
    ref = datetime(2026, 7, 31, tzinfo=UTC)  # Friday

    result = utils.get_next_sat(dt=ref)

    assert result.weekday() == _SATURDAY
    assert result >= ref

  def test_a_saturday_reference_is_its_own_last_and_next_saturday(self) -> None:
    ref = datetime(2026, 8, 1, tzinfo=UTC)  # Saturday

    assert utils.get_last_sat(dt=ref) == ref
    assert utils.get_next_sat(dt=ref) == ref


class TestHandleAddrlike:
  def test_plain_string_passes_through(self) -> None:
    assert utils.handle_addrlike("someone@example.test") == "someone@example.test"

  def test_address_object_passes_through(self) -> None:
    addr = Address(display_name="Someone", username="someone", domain="example.test")

    assert utils.handle_addrlike(addr) is addr

  def test_four_tuple_is_converted_to_an_address_object(self) -> None:
    result = utils.handle_addrlike(("Someone", "someone", "example.test", None))

    assert isinstance(result, Address)
    assert result.username == "someone"
    assert result.domain == "example.test"

  def test_unsupported_type_raises_type_error(self) -> None:
    with pytest.raises(TypeError):
      utils.handle_addrlike(12345)  # pyright: ignore[reportArgumentType]


class TestHandleAddrlikeSequence:
  def test_single_string_is_treated_as_one_address(self) -> None:
    result = utils.handle_addrlike_sequence("someone@example.test")

    assert result == "someone@example.test"

  def test_sequence_of_strings_is_converted_to_a_tuple(self) -> None:
    result = utils.handle_addrlike_sequence(["a@example.test", "b@example.test"])

    assert result == ("a@example.test", "b@example.test")

  def test_four_tuple_is_treated_as_one_packed_address_not_four_addresses(self) -> None:
    packed = ("Someone", "someone", "example.test", None)

    result = utils.handle_addrlike_sequence(packed)

    assert isinstance(result, Address)

  def test_unsupported_type_raises_type_error(self) -> None:
    with pytest.raises(TypeError):
      utils.handle_addrlike_sequence(12345)  # pyright: ignore[reportArgumentType]


class TestHandleAttachment:
  def test_known_extension_gets_the_correct_mime_type(self, tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("hello")

    content, info = utils.handle_attachment(file)

    assert content == b"hello"
    assert info == {"maintype": "text", "subtype": "plain", "filename": "notes.txt"}

  def test_unknown_extension_falls_back_to_octet_stream(self, tmp_path: Path) -> None:
    file = tmp_path / "mystery.unknownext"
    file.write_bytes(b"\x00\x01")

    _content, info = utils.handle_attachment(file)

    assert info["maintype"] == "application"
    assert info["subtype"] == "octet-stream"


class TestPrepareEmailMessage:
  def test_builds_a_minimal_message(self) -> None:
    parts: EmailMessageParts = {
      "subject": "Hello",
      "body": "World",
      "from_addr": "from@example.test",
      "to_addrs": "to@example.test",
    }

    msg = utils.prepare_email_message(parts)

    assert isinstance(msg, EmailMessage)
    assert msg["Subject"] == "Hello"
    assert msg.get_content().strip() == "World"
    assert msg["From"] == "from@example.test"
    assert msg["To"] == "to@example.test"

  def test_cc_and_bcc_are_optional(self) -> None:
    base_parts: EmailMessageParts = {
      "subject": "Hello",
      "body": "World",
      "from_addr": "from@example.test",
      "to_addrs": "to@example.test",
    }

    msg_without = utils.prepare_email_message(base_parts)
    assert msg_without["Cc"] is None
    assert msg_without["Bcc"] is None

    with_cc_bcc: EmailMessageParts = {**base_parts, "cc_addrs": "cc@example.test", "bcc_addrs": "bcc@example.test"}
    msg_with = utils.prepare_email_message(with_cc_bcc)
    assert msg_with["Cc"] == "cc@example.test"
    assert msg_with["Bcc"] == "bcc@example.test"

  def test_single_attachment(self, tmp_path: Path) -> None:
    attachment = tmp_path / "file.txt"
    attachment.write_text("attachment content")
    parts: EmailMessageParts = {
      "subject": "Hello",
      "body": "World",
      "from_addr": "from@example.test",
      "to_addrs": "to@example.test",
      "attachments": attachment,
    }

    msg = utils.prepare_email_message(parts)

    payload_filenames = [part.get_filename() for part in msg.iter_attachments()]
    assert payload_filenames == ["file.txt"]

  def test_sequence_of_attachments(self, tmp_path: Path) -> None:
    attachment_a = tmp_path / "a.txt"
    attachment_a.write_text("a")
    attachment_b = tmp_path / "b.txt"
    attachment_b.write_text("b")
    parts: EmailMessageParts = {
      "subject": "Hello",
      "body": "World",
      "from_addr": "from@example.test",
      "to_addrs": "to@example.test",
      "attachments": [attachment_a, attachment_b],
    }

    msg = utils.prepare_email_message(parts)

    payload_filenames = {part.get_filename() for part in msg.iter_attachments()}
    assert payload_filenames == {"a.txt", "b.txt"}


class _FakeSMTP:
  instances: ClassVar[list[_FakeSMTP]] = []

  def __init__(self, server: str, port: int) -> None:
    self.server = server
    self.port = port
    self.ehlo_calls = 0
    self.starttls_called = False
    self.login_args: tuple[object, object] | None = None
    self.sent_messages: list[EmailMessage] = []
    type(self).instances.append(self)

  def ehlo(self) -> None:
    self.ehlo_calls += 1

  def starttls(self, context: object = None) -> None:
    self.starttls_called = True

  def login(self, user: object, password: object) -> None:
    self.login_args = (user, password)

  def send_message(self, msg: EmailMessage) -> None:
    self.sent_messages.append(msg)

  def __enter__(self) -> Self:
    return self

  def __exit__(self, *exc_info: object) -> None:
    pass


class TestBatchSendEmails:
  @pytest.fixture(autouse=True)
  def _fake_smtp(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    _FakeSMTP.instances = []
    monkeypatch.setattr(utils, "SMTP", _FakeSMTP)
    yield

  def test_sends_a_single_message_using_explicit_credentials(self) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Hi"

    utils.batch_send_emails(
      msg,
      smtp_server="smtp.example.test",
      smtp_port=_TEST_SMTP_PORT,
      smtp_user="user@example.test",
      smtp_password=SecretStr("secret"),
    )

    (server_instance,) = _FakeSMTP.instances
    assert server_instance.server == "smtp.example.test"
    assert server_instance.port == _TEST_SMTP_PORT
    assert server_instance.login_args == ("user@example.test", "secret")
    assert server_instance.sent_messages == [msg]
    assert server_instance.starttls_called

  def test_sends_a_sequence_of_messages(self) -> None:
    msg_a = EmailMessage()
    msg_b = EmailMessage()

    utils.batch_send_emails(
      [msg_a, msg_b],
      smtp_server="smtp.example.test",
      smtp_port=_TEST_SMTP_PORT,
      smtp_user="user@example.test",
      smtp_password=SecretStr("secret"),
    )

    (server_instance,) = _FakeSMTP.instances
    assert server_instance.sent_messages == [msg_a, msg_b]

  def test_address_object_smtp_user_is_stringified(self) -> None:
    msg = EmailMessage()
    addr = Address(display_name="User", username="user", domain="example.test")

    utils.batch_send_emails(
      msg, smtp_server="smtp.example.test", smtp_port=_TEST_SMTP_PORT, smtp_user=addr, smtp_password=SecretStr("secret")
    )

    (server_instance,) = _FakeSMTP.instances
    assert server_instance.login_args is not None
    login_user, _login_pwd = server_instance.login_args
    assert login_user == str(addr)
