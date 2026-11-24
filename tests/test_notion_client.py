"""Tests for NotionRecruitmentClient matching, updating, creating, and regression guards."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from recruit_mail_observer.models import ClassificationResult, EmailMessage
from recruit_mail_observer.notion_client import NotionRecruitmentClient, normalize_company_name

ROOT = Path(__file__).resolve().parents[1]


class FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._bytes

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _email(received_date: str = "2026-11-24") -> EmailMessage:
    return EmailMessage(
        uid="1",
        message_id="<m1@test.com>",
        message_hash="c" * 64,
        sender_name="채용팀",
        sender_email="hr@test.com",
        subject="전형 안내",
        received_at=f"{received_date}T10:00:00+09:00",
        received_date=received_date,
        body_text="본문",
    )


def _classification(company: str, stage: str, outcome: str = "IN_PROGRESS", role: str = "데이터 엔지니어") -> ClassificationResult:
    return ClassificationResult(
        is_recruitment_process=True,
        company_name=company,
        role=role,
        final_stage=stage,
        outcome=outcome,
        summary="핵심 요약",
        confidence=0.92,
        source="llm",
        needs_review=False,
    )


class TestNotionRecruitmentClient(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
        self.rules["company_aliases"] = {"비바리퍼블리카": "토스"}
        self.schema = json.loads((ROOT / "config" / "notion_schema.json").read_text(encoding="utf-8"))

    def test_normalize_company_name_applies_aliases(self) -> None:
        aliases = self.rules["company_aliases"]
        self.assertEqual(normalize_company_name("비바리퍼블리카", aliases), "토스")
        self.assertEqual(normalize_company_name("(주) 토스", aliases), "토스")

    def test_updates_existing_row_matched_by_alias(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            body = json.loads(req.data.decode("utf-8")) if req.data else {}
            calls.append((req.get_method(), req.full_url, body))
            if req.full_url.endswith("/query"):
                return FakeHttpResponse(
                    {
                        "results": [
                            {
                                "id": "page-1",
                                "url": "https://notion.so/page-1",
                                "properties": {
                                    "Name": {"title": [{"plain_text": "토스"}]},
                                    "상태": {"select": {"name": "진행중"}},
                                    "최종전형": {"select": {"name": "서류"}},
                                    "업데이트": {"date": {"start": "2026-11-20"}},
                                    "직무": {"select": None},
                                },
                            }
                        ],
                        "has_more": False,
                    }
                )
            if "/pages/page-1" in req.full_url:
                return FakeHttpResponse({"id": "page-1", "url": "https://notion.so/page-1"})
            return FakeHttpResponse({"object": "list"})

        client = NotionRecruitmentClient(
            token="ntn_test",
            database_id="db-1",
            notion_schema=self.schema,
            company_aliases=self.rules["company_aliases"],
            http_opener=fake_opener,
        )
        res = client.sync(_email("2026-11-24"), _classification("비바리퍼블리카", "1차면접"))

        self.assertEqual(res.action, "updated")
        self.assertEqual(res.matched_company, "토스")
        patch_calls = [c for c in calls if c[0] == "PATCH" and "/pages/page-1" in c[1]]
        self.assertEqual(len(patch_calls), 1)
        props = patch_calls[0][2]["properties"]
        self.assertEqual(props["상태"]["select"]["name"], "진행중")
        self.assertEqual(props["최종전형"]["select"]["name"], "1차면접")
        self.assertEqual(props["업데이트"]["date"]["start"], "2026-11-24")
        self.assertEqual(props["직무"]["select"]["name"], "데이터 엔지니어")

    def test_creates_new_row_when_unmatched(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            body = json.loads(req.data.decode("utf-8")) if req.data else {}
            calls.append((req.get_method(), req.full_url, body))
            if req.full_url.endswith("/query"):
                return FakeHttpResponse({"results": [], "has_more": False})
            if req.full_url.endswith("/pages"):
                return FakeHttpResponse({"id": "page-new", "url": "https://notion.so/page-new"})
            return FakeHttpResponse({"object": "list"})

        client = NotionRecruitmentClient(
            token="ntn_test",
            database_id="db-1",
            notion_schema=self.schema,
            company_aliases=self.rules["company_aliases"],
            http_opener=fake_opener,
        )
        res = client.sync(_email("2026-11-24"), _classification("신규회사", "과제/테스트"))

        self.assertEqual(res.action, "created")
        self.assertEqual(res.page_id, "page-new")
        post_pages = [c for c in calls if c[0] == "POST" and c[1].endswith("/pages")]
        self.assertEqual(len(post_pages), 1)
        props = post_pages[0][2]["properties"]
        self.assertEqual(props["전형 시작일"]["date"]["start"], "2026-11-24")
        self.assertEqual(props["업데이트"]["date"]["start"], "2026-11-24")
        self.assertEqual(props["상태"]["select"]["name"], "진행중")
        self.assertEqual(props["최종전형"]["select"]["name"], "과제/테스트")
        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "신규회사")

    def test_skips_regression_from_terminal_stage(self) -> None:
        def fake_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            if req.full_url.endswith("/query"):
                return FakeHttpResponse(
                    {
                        "results": [
                            {
                                "id": "page-2",
                                "url": "https://notion.so/page-2",
                                "properties": {
                                    "Name": {"title": [{"plain_text": "당근"}]},
                                    "상태": {"select": {"name": "합격"}},
                                    "최종전형": {"select": {"name": "처우협의"}},
                                    "업데이트": {"date": {"start": "2026-11-24"}},
                                    "직무": {"select": {"name": "데이터 엔지니어"}},
                                },
                            }
                        ],
                        "has_more": False,
                    }
                )
            raise AssertionError("Should not update or append on regression")

        client = NotionRecruitmentClient(
            token="ntn_test",
            database_id="db-1",
            notion_schema=self.schema,
            http_opener=fake_opener,
        )
        res = client.sync(_email("2026-11-22"), _classification("당근", "서류"))
        self.assertEqual(res.action, "skipped")
        self.assertEqual(res.note, "skipped_stage_regression")


if __name__ == "__main__":
    unittest.main()
