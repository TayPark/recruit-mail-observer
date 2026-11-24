"""Tests for SlackNotifier payload formatting, color sidebar, action buttons, and delivery."""

from __future__ import annotations

import json
import unittest
from typing import Any

from recruit_mail_observer.models import ClassificationResult, EmailMessage, NotionSyncResult
from recruit_mail_observer.slack_notifier import SlackNotifier, build_mail_url


class FakeHttpResponse:
    status = 200

    def read(self) -> bytes:
        return b"ok"

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class TestSlackNotifier(unittest.TestCase):
    def test_builds_colored_attachment_with_action_buttons_and_deadline(self) -> None:
        sent: dict[str, Any] = {}

        def fake_opener(req: Any, timeout: int = 15) -> FakeHttpResponse:
            sent["url"] = req.full_url
            sent["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeHttpResponse()

        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.com/services/T/B/X",
            mail_provider="kakao",
            http_opener=fake_opener,
        )
        email_msg = EmailMessage(
            uid="1",
            message_id="<m@test.com>",
            message_hash="d" * 64,
            sender_name="채용팀",
            sender_email="hr@test.com",
            subject="[노바랩스] 사전 과제 안내",
            received_at="2026-11-24T12:00:00+09:00",
            received_date="2026-11-24",
            body_text="11/28까지 사전 과제 제출 요청.",
        )
        classification = ClassificationResult(
            is_recruitment_process=True,
            company_name="노바랩스",
            role="프로덕트 매니저",
            final_stage="과제/테스트",
            outcome="IN_PROGRESS",
            summary="11/28까지 온라인 사전 과제 제출 요청.",
            confidence=0.55,
            source="rule_fallback",
            needs_review=True,
            next_deadline="2026-11-28",
        )
        notion_res = NotionSyncResult(
            action="created",
            page_id="p-1",
            page_url="https://notion.so/p-1",
            matched_company="노바랩스",
            applied_stage="과제/테스트",
        )

        notifier.notify(email_msg, classification, notion_res)
        payload = sent["payload"]
        self.assertIn("[진행중] 노바랩스 - 과제/테스트", payload["text"])

        attachment = payload["attachments"][0]
        self.assertEqual(attachment["color"], "#36c5f0")
        blocks = attachment["blocks"]

        fields_text = [f["text"] for f in blocks[1]["fields"]]
        self.assertTrue(any("2026-11-28" in t for t in fields_text))

        actions = blocks[3]["elements"]
        action_urls = [a["url"] for a in actions]
        self.assertTrue(any("https://mail.kakao.com/top/SEARCH?folderId=INBOX&subject=" in u for u in action_urls))
        self.assertIn("https://notion.so/p-1", action_urls)

        context_texts = [e["text"] for e in blocks[-1]["elements"]]
        self.assertTrue(any("Notion 신규 등록" in t for t in context_texts))
        self.assertTrue(any("수동 확인 필요" in t for t in context_texts))

    def test_build_mail_url_supports_gmail_and_naver(self) -> None:
        self.assertIn("mail.google.com/mail/u/0/#search/", build_mail_url("Interview", provider="gmail"))
        self.assertEqual(build_mail_url("Interview", provider="naver"), "https://mail.naver.com/")

    def test_escapes_mrkdwn_injection_in_subject_and_summary(self) -> None:
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/services/T/B/X")
        email_msg = EmailMessage(
            uid="2",
            message_id="<m2@test.com>",
            message_hash="e" * 64,
            sender_name="HR",
            sender_email="hr@test.com",
            subject="<!channel> <https://evil.test|click> & test",
            received_at="2026-11-24T12:00:00+09:00",
            received_date="2026-11-24",
            body_text="body",
        )
        cls = ClassificationResult(
            is_recruitment_process=True,
            company_name="A&B",
            role="PM",
            final_stage="1차면접",
            outcome="IN_PROGRESS",
            summary="Check <!here> & <https://bad.test>",
            confidence=0.9,
            source="llm",
        )
        notion_res = NotionSyncResult(
            action="updated",
            page_id="p-1",
            page_url="https://notion.so/p-1",
            matched_company="A&B",
            applied_stage="1차면접",
        )
        payload = notifier.build_payload(email_msg, cls, notion_res)
        summary_block = payload["attachments"][0]["blocks"][2]["text"]["text"]
        self.assertIn("&lt;!here&gt; &amp; &lt;https://bad.test&gt;", summary_block)
        self.assertNotIn("<!here>", summary_block)


if __name__ == "__main__":
    unittest.main()
