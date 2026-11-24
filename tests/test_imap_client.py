"""Tests for KakaoImapClient and MIME/HTML parsing."""

from __future__ import annotations

import ssl
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as StdEmailMessage
from unittest import mock

from recruit_mail_observer.imap_client import (
    KakaoImapClient,
    create_verified_imap_connection,
    parse_raw_email,
)

KST = timezone(timedelta(hours=9))


class FakeImap:
    def __init__(self, raw_emails: dict[str, bytes]) -> None:
        self.raw_emails = raw_emails
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (user, password)))
        return "OK", [b"Logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", (mailbox, readonly)))
        return "OK", [b"1"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((f"uid:{command}", args))
        if command == "SEARCH":
            joined = " ".join(self.raw_emails.keys()).encode("ascii")
            return "OK", [joined]
        if command == "FETCH":
            uid = str(args[0])
            return "OK", [(b"1 (BODY[] {10})", self.raw_emails[uid])]
        raise AssertionError(f"Unexpected IMAP command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", ()))
        return "BYE", [b"Logged out"]


def _build_raw_html_email(subject: str, sender: str, html_body: str, message_id: str) -> bytes:
    msg = StdEmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "user@kakao.com"
    msg["Date"] = "Tue, 24 Nov 2026 10:30:00 +0900"
    msg["Message-ID"] = message_id
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg.as_bytes()


class TestKakaoImapClient(unittest.TestCase):
    def test_fetch_uses_readonly_and_body_peek(self) -> None:
        raw = _build_raw_html_email(
            subject="[토스] 서류 전형 결과 안내",
            sender="토스 채용팀 <recruit@toss.im>",
            html_body="<html><body><p>서류 전형 합격을 축하드립니다.</p><p>1차 면접 일정을 안내드립니다.</p></body></html>",
            message_id="<msg-1@toss.im>",
        )
        fake = FakeImap({"101": raw})
        client = KakaoImapClient(
            host="imap.kakao.com",
            port=993,
            email_address="user@kakao.com",
            app_password="app-password",
            imap_factory=lambda host, port: fake,
        )

        messages = client.fetch_unread_since(
            lookback_days=7,
            now=datetime(2026, 11, 24, 12, 0, tzinfo=KST),
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].subject, "[토스] 서류 전형 결과 안내")
        self.assertEqual(messages[0].sender_email, "recruit@toss.im")
        self.assertEqual(messages[0].received_date, "2026-11-24")
        self.assertIn("서류 전형 합격을 축하드립니다.", messages[0].body_text)
        self.assertIn(("select", ("INBOX", True)), fake.calls)
        self.assertIn(("uid:FETCH", ("101", "(BODY.PEEK[])")), fake.calls)

    def test_parse_raw_email_handles_missing_message_id_deterministically(self) -> None:
        msg = StdEmailMessage()
        msg["Subject"] = "면접 일정 안내"
        msg["From"] = "hr@example.com"
        msg.set_content("1차 면접 안내드립니다.")
        raw = msg.as_bytes()
        parsed1 = parse_raw_email("55", raw)
        parsed2 = parse_raw_email("55", raw)
        self.assertTrue(parsed1.message_id.startswith("<fallback-"))
        self.assertEqual(parsed1.message_hash, parsed2.message_hash)
        self.assertEqual(len(parsed1.message_hash), 64)

    def test_production_imap_factory_enforces_verified_tls_and_timeout(self) -> None:
        with mock.patch("recruit_mail_observer.imap_client.imaplib.IMAP4_SSL") as mock_imap:
            create_verified_imap_connection("imap.kakao.com", 993)
            mock_imap.assert_called_once()
            _, kwargs = mock_imap.call_args
            ctx = kwargs["ssl_context"]
            self.assertIsInstance(ctx, ssl.SSLContext)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
            self.assertGreater(kwargs["timeout"], 0)


if __name__ == "__main__":
    unittest.main()
