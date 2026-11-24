"""Tests for Stage 2 HybridClassifier (Gemini 4 Hydrogen Low + fallback)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from recruit_mail_observer.llm_classifier import HybridClassifier
from recruit_mail_observer.models import EmailMessage
from recruit_mail_observer.rule_filter import RuleFilter

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


def _sample_email() -> EmailMessage:
    return EmailMessage(
        uid="7",
        message_id="<msg-7@company.com>",
        message_hash="b" * 64,
        sender_name="토스 채용팀",
        sender_email="recruit@toss.im",
        subject="[토스] 1차 면접 일정 안내",
        received_at="2026-11-24T10:00:00+09:00",
        received_date="2026-11-24",
        body_text="데이터 엔지니어 포지션 1차 면접 일정을 회신해 주세요.",
    )


class TestHybridClassifier(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
        self.schema = json.loads((ROOT / "config" / "notion_schema.json").read_text(encoding="utf-8"))
        self.rule_filter = RuleFilter(self.rules, self.schema)

    def test_gemini_4_hydrogen_low_request_and_parsing(self) -> None:
        captured: dict[str, Any] = {}

        def fake_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeHttpResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "is_recruitment_process": True,
                                                "company_name": "토스",
                                                "role": "데이터 엔지니어",
                                                "final_stage": "1차면접",
                                                "outcome": "IN_PROGRESS",
                                                "summary": "서류 통과 후 1차 면접 일정 조율 요청.",
                                                "confidence": 0.95,
                                                "reason": "1차 면접 일정 안내 메일",
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        classifier = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=fake_opener,
        )

        result = classifier.classify(_sample_email())

        self.assertIn("models/gemini-4-hydrogen:generateContent", captured["url"])
        self.assertEqual(captured["body"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
        self.assertEqual(captured["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertTrue(result.is_recruitment_process)
        self.assertEqual(result.company_name, "토스")
        self.assertEqual(result.final_stage, "1차면접")
        self.assertEqual(result.source, "llm")
        self.assertFalse(result.needs_review)

    def test_invalid_stage_is_normalized_and_flagged_for_review(self) -> None:
        def fake_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            return FakeHttpResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "is_recruitment_process": True,
                                                "company_name": "토스",
                                                "role": "",
                                                "final_stage": "존재하지않는단계",
                                                "outcome": "IN_PROGRESS",
                                                "summary": "요약",
                                                "confidence": 0.9,
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        classifier = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=fake_opener,
        )
        result = classifier.classify(_sample_email())
        self.assertEqual(result.final_stage, "서류")
        self.assertTrue(result.needs_review)

    def test_api_error_falls_back_to_rules(self) -> None:
        def failing_opener(req: Any, timeout: int = 30) -> FakeHttpResponse:
            raise OSError("network timeout")

        classifier = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=failing_opener,
        )
        result = classifier.classify(_sample_email())
        self.assertEqual(result.source, "rule_fallback")
        self.assertEqual(result.final_stage, "1차면접")
        self.assertTrue(result.needs_review)

    def test_retries_once_on_timeout_then_succeeds(self) -> None:
        attempts = {"count": 0}

        def flaky_opener(req: Any, timeout: int = 60) -> FakeHttpResponse:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("The read operation timed out")
            return FakeHttpResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "is_recruitment_process": True,
                                                "company_name": "토스",
                                                "role": "데이터 엔지니어",
                                                "final_stage": "1차면접",
                                                "outcome": "IN_PROGRESS",
                                                "summary": "1차 면접 일정 안내.",
                                                "confidence": 0.94,
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        classifier = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=flaky_opener,
            max_retries=1,
        )
        result = classifier.classify(_sample_email())
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.final_stage, "1차면접")

    def test_rejects_malformed_boolean_nan_confidence_and_invalid_date(self) -> None:
        def bad_bool_opener(req: Any, timeout: int = 60) -> FakeHttpResponse:
            return FakeHttpResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "is_recruitment_process": "false",
                                                "company_name": "토스",
                                                "final_stage": "1차면접",
                                                "outcome": "IN_PROGRESS",
                                                "confidence": "NaN",
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        classifier = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=bad_bool_opener,
            max_retries=0,
        )
        res = classifier.classify(_sample_email())
        self.assertEqual(res.source, "rule_fallback")
        self.assertTrue(res.needs_review)

        def bad_date_opener(req: Any, timeout: int = 60) -> FakeHttpResponse:
            return FakeHttpResponse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "is_recruitment_process": True,
                                                "company_name": "토스",
                                                "final_stage": "1차면접",
                                                "outcome": "IN_PROGRESS",
                                                "next_deadline": "2026-02-31",
                                                "summary": "요약",
                                                "confidence": 0.9,
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        classifier2 = HybridClassifier(
            provider="gemini",
            api_key="test-key",
            model="gemini-4-hydrogen",
            thinking_level="low",
            min_confidence=0.65,
            notion_schema=self.schema,
            rule_filter=self.rule_filter,
            http_opener=bad_date_opener,
            max_retries=0,
        )
        res2 = classifier2.classify(_sample_email())
        self.assertEqual(res2.next_deadline, "")


if __name__ == "__main__":
    unittest.main()
