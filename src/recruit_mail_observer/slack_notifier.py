"""Slack Incoming Webhook notifier with escaped mrkdwn, color sidebar, and action buttons."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

from recruit_mail_observer.models import ClassificationResult, EmailMessage, NotionSyncResult

OUTCOME_BADGE = {
    "PASS": "[합격]",
    "FAIL": "[불합격]",
    "IN_PROGRESS": "[진행중]",
    "CLOSED": "[종료]",
}

OUTCOME_COLOR = {
    "PASS": "#2eb886",
    "FAIL": "#e01e5a",
    "IN_PROGRESS": "#36c5f0",
    "CLOSED": "#8c8c8c",
}


def escape_mrkdwn(text: str) -> str:
    """Escape Slack mrkdwn special characters to prevent accidental mentions or link injection."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_mail_url(subject: str, *, provider: str = "kakao", folder_id: str = "INBOX") -> str:
    encoded_subject = urllib.parse.quote(subject.strip(), safe="")
    provider_norm = provider.strip().lower()
    if provider_norm == "gmail":
        return f"https://mail.google.com/mail/u/0/#search/{encoded_subject}"
    if provider_norm == "naver":
        return "https://mail.naver.com/"
    return f"https://mail.kakao.com/top/SEARCH?folderId={folder_id}&subject={encoded_subject}"


def build_kakao_mail_url(subject: str, *, folder_id: str = "INBOX") -> str:
    return build_mail_url(subject, provider="kakao", folder_id=folder_id)


class SlackNotifier:
    """Sends formatted recruitment updates to a Slack Incoming Webhook."""

    def __init__(
        self,
        *,
        webhook_url: str,
        mail_provider: str = "kakao",
        http_opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: int = 15,
    ) -> None:
        self.webhook_url = webhook_url
        self.mail_provider = mail_provider
        self._http_opener = http_opener
        self.timeout_seconds = timeout_seconds

    def build_payload(
        self,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        notion_result: NotionSyncResult,
    ) -> dict[str, Any]:
        badge = OUTCOME_BADGE.get(classification.outcome, "[진행중]")
        color = OUTCOME_COLOR.get(classification.outcome, "#36c5f0")
        company = (notion_result.matched_company or classification.company_name)[:80]
        header_text = f"{badge} {company} - {classification.final_stage}"[:150]

        if notion_result.note == "needs_review":
            notion_tag = "Notion 보류(수동 확인 필요)"
        elif notion_result.note == "ambiguous_match":
            notion_tag = "Notion 보류(동일 회사 복수 행)"
        elif notion_result.note == "possible_reapplication_review":
            notion_tag = "Notion 보류(재지원 가능성 확인 필요)"
        else:
            notion_tag = {
                "created": "Notion 신규 등록",
                "updated": "Notion 상태 업데이트",
                "skipped": "Notion 변경 스킵(기존 단계 유지)",
            }.get(notion_result.action, "Notion 처리 완료")

        role_display = (classification.role or "미지정")[:80]
        summary_raw = (classification.summary or email_msg.subject)[:500]
        summary_escaped = escape_mrkdwn(summary_raw)
        subject_escaped = escape_mrkdwn(email_msg.subject[:100]).replace("|", "/")

        fields: list[dict[str, str]] = [
            {"type": "mrkdwn", "text": f"*회사*\n{escape_mrkdwn(company)}"},
            {"type": "mrkdwn", "text": f"*최종전형*\n{escape_mrkdwn(classification.final_stage)}"},
            {"type": "mrkdwn", "text": f"*직무*\n{escape_mrkdwn(role_display)}"},
            {"type": "mrkdwn", "text": f"*수신일시*\n{escape_mrkdwn(email_msg.received_at)}"},
        ]
        if classification.next_deadline:
            fields.append(
                {"type": "mrkdwn", "text": f"*다음 일정*\n{escape_mrkdwn(classification.next_deadline)}"}
            )

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text},
            },
            {
                "type": "section",
                "fields": fields,
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*요약*\n{summary_escaped}"},
            },
        ]

        mail_url = build_mail_url(email_msg.subject, provider=self.mail_provider)
        action_elements: list[dict[str, Any]] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "메일에서 보기"},
                "url": mail_url,
            }
        ]
        if notion_result.page_url:
            action_elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Notion에서 열기"},
                    "url": notion_result.page_url,
                }
            )
        blocks.append({"type": "actions", "elements": action_elements})

        context_elements: list[dict[str, Any]] = [
            {"type": "mrkdwn", "text": f"메일: <{mail_url}|{subject_escaped}>"},
            {"type": "mrkdwn", "text": f"처리: *{notion_tag}*"},
        ]
        if classification.needs_review or notion_result.note in {
            "needs_review",
            "ambiguous_match",
            "possible_reapplication_review",
        }:
            context_elements.append(
                {
                    "type": "mrkdwn",
                    "text": f"*수동 확인 필요* (source={classification.source}, conf={classification.confidence:.2f})",
                }
            )
        blocks.append({"type": "context", "elements": context_elements})

        return {
            "text": f"{header_text} | {summary_raw[:200]}",
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
        }

    def notify(
        self,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        notion_result: NotionSyncResult,
    ) -> None:
        payload = self.build_payload(email_msg, classification, notion_result)
        req = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._http_opener(req, timeout=self.timeout_seconds) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise RuntimeError(f"Slack webhook returned HTTP {status}")
