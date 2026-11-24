"""End-to-end mocked pipeline tests including deduplication and dry-run."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from recruit_mail_observer.models import ClassificationResult, EmailMessage, NotionSyncResult
from recruit_mail_observer.pipeline import RecruitMailPipeline
from recruit_mail_observer.rule_filter import RuleFilter
from recruit_mail_observer.state_store import StateStore

ROOT = Path(__file__).resolve().parents[1]


class StubImap:
    def __init__(self, messages: list[EmailMessage]) -> None:
        self.messages = messages

    def fetch_unread_since(self, *, lookback_days: int = 7, max_emails: Optional[int] = None) -> list[EmailMessage]:
        return self.messages[:max_emails] if max_emails else list(self.messages)


class StubClassifier:
    def __init__(self, mapping: dict[str, ClassificationResult]) -> None:
        self.mapping = mapping

    def classify(self, email_msg: EmailMessage) -> ClassificationResult:
        return self.mapping[email_msg.uid]


class StubNotion:
    def __init__(self) -> None:
        self.synced: list[str] = []

    def sync(self, email_msg: EmailMessage, classification: ClassificationResult) -> NotionSyncResult:
        self.synced.append(email_msg.uid)
        return NotionSyncResult(
            action="updated",
            page_id="page-1",
            page_url="https://notion.so/page-1",
            matched_company=classification.company_name,
            applied_stage=classification.final_stage,
        )


class StubSlack:
    def __init__(self) -> None:
        self.notified: list[str] = []

    def notify(
        self,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        notion_result: NotionSyncResult,
    ) -> None:
        self.notified.append(email_msg.uid)


class TestRecruitMailPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
        self.schema = json.loads((ROOT / "config" / "notion_schema.json").read_text(encoding="utf-8"))
        self.rule_filter = RuleFilter(self.rules, self.schema)
        self.messages = [
            EmailMessage(
                uid="1",
                message_id="<ad@wanted.com>",
                message_hash="1" * 64,
                sender_name="원티드",
                sender_email="noreply@wantedlab.com",
                subject="[원티드] 이번 주 추천 포지션",
                received_at="2026-11-24T09:00:00+09:00",
                received_date="2026-11-24",
                body_text="추천 포지션을 확인하세요.",
            ),
            EmailMessage(
                uid="2",
                message_id="<toss@greetinghr.com>",
                message_hash="2" * 64,
                sender_name="토스 채용팀",
                sender_email="noreply@greetinghr.com",
                subject="[토스] 서류 전형 결과 안내",
                received_at="2026-11-24T10:00:00+09:00",
                received_date="2026-11-24",
                body_text="서류 전형 결과 1차 면접 대상자로 선정되셨습니다.",
            ),
        ]
        self.classifications = {
            "2": ClassificationResult(
                is_recruitment_process=True,
                company_name="토스",
                role="데이터 엔지니어",
                final_stage="1차면접",
                outcome="IN_PROGRESS",
                summary="서류 통과 후 1차 면접 안내.",
                confidence=0.96,
                source="llm",
                needs_review=False,
            )
        }

    def test_dry_run_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            notion = StubNotion()
            slack = StubSlack()
            pipeline = RecruitMailPipeline(
                imap_client=StubImap(self.messages),  # type: ignore[arg-type]
                state_store=StateStore(state_path),
                rule_filter=self.rule_filter,
                classifier=StubClassifier(self.classifications),  # type: ignore[arg-type]
                notion_client=notion,  # type: ignore[arg-type]
                slack_notifier=slack,  # type: ignore[arg-type]
            )
            report, events = pipeline.run(dry_run=True)

            self.assertEqual(report.scanned_unread, 2)
            self.assertEqual(report.filtered_non_candidate, 1)
            self.assertEqual(report.recruitment_detected, 1)
            self.assertEqual(len(events), 1)
            self.assertEqual(notion.synced, [])
            self.assertEqual(slack.notified, [])
            self.assertFalse(state_path.exists())

    def test_live_run_syncs_once_and_deduplicates_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            notion = StubNotion()
            slack = StubSlack()
            pipeline = RecruitMailPipeline(
                imap_client=StubImap(self.messages),  # type: ignore[arg-type]
                state_store=StateStore(state_path),
                rule_filter=self.rule_filter,
                classifier=StubClassifier(self.classifications),  # type: ignore[arg-type]
                notion_client=notion,  # type: ignore[arg-type]
                slack_notifier=slack,  # type: ignore[arg-type]
            )

            report1, _ = pipeline.run(dry_run=False)
            self.assertEqual(report1.recruitment_detected, 1)
            self.assertEqual(report1.notion_updated, 1)
            self.assertEqual(report1.slack_notified, 1)
            self.assertEqual(notion.synced, ["2"])
            self.assertEqual(slack.notified, ["2"])

            report2, events2 = pipeline.run(dry_run=False)
            self.assertEqual(report2.skipped_already_processed, 2)
            self.assertEqual(report2.recruitment_detected, 0)
            self.assertEqual(len(events2), 0)
            self.assertEqual(notion.synced, ["2"])
            self.assertEqual(slack.notified, ["2"])

    def test_needs_review_skips_notion_write_but_notifies_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            notion = StubNotion()
            slack = StubSlack()
            review_cls = {
                "2": ClassificationResult(
                    is_recruitment_process=True,
                    company_name="토스",
                    role="",
                    final_stage="서류",
                    outcome="IN_PROGRESS",
                    summary="저신뢰도 결과",
                    confidence=0.4,
                    source="rule_fallback",
                    needs_review=True,
                )
            }
            pipeline = RecruitMailPipeline(
                imap_client=StubImap([self.messages[1]]),  # type: ignore[arg-type]
                state_store=StateStore(state_path),
                rule_filter=self.rule_filter,
                classifier=StubClassifier(review_cls),  # type: ignore[arg-type]
                notion_client=notion,  # type: ignore[arg-type]
                slack_notifier=slack,  # type: ignore[arg-type]
            )
            report, _ = pipeline.run(dry_run=False)
            self.assertEqual(report.notion_skipped, 1)
            self.assertEqual(notion.synced, [])
            self.assertEqual(slack.notified, ["2"])

    def test_partial_slack_failure_resumes_without_replaying_notion(self) -> None:
        class FlakySlack(StubSlack):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def notify(self, email_msg: EmailMessage, classification: ClassificationResult, notion_result: NotionSyncResult) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("slack down")
                super().notify(email_msg, classification, notion_result)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            notion = StubNotion()
            slack = FlakySlack()
            pipeline = RecruitMailPipeline(
                imap_client=StubImap([self.messages[1]]),  # type: ignore[arg-type]
                state_store=StateStore(state_path),
                rule_filter=self.rule_filter,
                classifier=StubClassifier(self.classifications),  # type: ignore[arg-type]
                notion_client=notion,  # type: ignore[arg-type]
                slack_notifier=slack,  # type: ignore[arg-type]
            )
            report1, _ = pipeline.run(dry_run=False)
            self.assertEqual(len(report1.errors), 1)
            self.assertEqual(notion.synced, ["2"])
            self.assertEqual(slack.notified, [])

            report2, _ = pipeline.run(dry_run=False)
            self.assertEqual(report2.errors, [])
            self.assertEqual(notion.synced, ["2"])  # NOT replayed!
            self.assertEqual(slack.notified, ["2"])


if __name__ == "__main__":
    unittest.main()
