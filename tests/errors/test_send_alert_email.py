"""Tests for `aeth_ext.errors.send_alert_email.send_alert_email`."""

# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from aeth_ext.errors import send_alert_email as sae_mod

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence
  from email.message import EmailMessage


class TestNoRecipientsConfigured:
  def test_warns_and_returns_without_sending(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(sae_mod.SETTINGS, "alerts_recipients", frozenset())
    calls: list[object] = []
    monkeypatch.setattr(sae_mod, "batch_send_emails", lambda **kwargs: calls.append(kwargs))

    with caplog.at_level("WARNING"):
      sae_mod.send_alert_email("subject", "content")

    assert calls == []
    assert any("Skipping alert email" in record.message for record in caplog.records)


class TestRecipientsConfigured:
  @pytest.fixture
  def _sent_messages(self, monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    monkeypatch.setattr(sae_mod.SETTINGS, "alerts_recipients", frozenset({"recipient@example.test"}))
    sent: list[EmailMessage] = []

    def fake_batch_send_emails(email_messages: EmailMessage | Sequence[EmailMessage], **kwargs: object) -> None:
      sent.extend(email_messages if isinstance(email_messages, list) else [email_messages])  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(sae_mod, "batch_send_emails", fake_batch_send_emails)
    return sent

  def test_builds_and_sends_a_message_with_the_subject_and_recipients(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Something broke", "the traceback")

    (msg,) = _sent_messages
    assert msg["Subject"] == "Something broke"
    assert msg["To"] == "recipient@example.test"

  def test_attaches_content_with_a_utf8_bom(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Subject", "the actual traceback content")

    (msg,) = _sent_messages
    (attachment,) = list(msg.iter_attachments())
    payload = attachment.get_content()

    assert payload.startswith("﻿")
    assert "the actual traceback content" in payload
    assert attachment.get_filename() == "alert.txt"

  def test_batch_send_emails_failure_is_caught_and_logged_not_propagated(
    self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
    monkeypatch.setattr(sae_mod.SETTINGS, "alerts_recipients", frozenset({"recipient@example.test"}))

    def raising_batch_send_emails(**kwargs: object) -> None:
      raise ConnectionError("smtp is down")

    monkeypatch.setattr(sae_mod, "batch_send_emails", raising_batch_send_emails)

    with caplog.at_level("ERROR"):
      sae_mod.send_alert_email("Subject", "content")  # must not raise

    assert any("Failed to send alert email" in record.message for record in caplog.records)

  def test_no_html_alternative_or_attachment_count_change_when_image_is_none(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Subject", "content")

    (msg,) = _sent_messages
    assert msg.get_body(preferencelist=("html",)) is None
    assert len(list(msg.iter_attachments())) == 1

  def test_embeds_the_image_inline_via_a_matching_cid_when_provided(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Subject", "content", image=b"fake-png-bytes")

    (msg,) = _sent_messages
    html_part = msg.get_body(preferencelist=("html",))
    assert html_part is not None
    assert f"cid:{sae_mod._IMAGE_CID}" in html_part.get_content()  # pyright: ignore[reportPrivateUsage]

    (image_part,) = (part for part in msg.walk() if part.get_content_type() == "image/png")
    assert image_part.get_content_disposition() == "inline"
    assert image_part.get("Content-ID") == f"<{sae_mod._IMAGE_CID}>"  # pyright: ignore[reportPrivateUsage]
    assert image_part.get_payload(decode=True) == b"fake-png-bytes"

  def test_inline_image_is_not_returned_by_iter_attachments(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Subject", "content", image=b"fake-png-bytes")

    (msg,) = _sent_messages
    # Only alert.txt -- the inline image has Content-Disposition: inline, so
    # iter_attachments() (which is about downloadable attachments) skips it.
    (attachment,) = msg.iter_attachments()
    assert attachment.get_filename() == "alert.txt"

  def test_text_attachment_is_still_present_alongside_the_image(self, _sent_messages: list[EmailMessage]) -> None:
    sae_mod.send_alert_email("Subject", "the actual traceback content", image=b"fake-png-bytes")

    (msg,) = _sent_messages
    text_attachment = next(a for a in msg.iter_attachments() if a.get_filename() == "alert.txt")
    assert "the actual traceback content" in text_attachment.get_content()
