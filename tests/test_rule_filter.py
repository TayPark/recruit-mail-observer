"""Tests for Stage 1 rule filter and rule-based fallback classifier."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recruit_mail_observer.models import EmailMessage
from recruit_mail_observer.rule_filter import RuleFilter

ROOT = Path(__file__).resolve().parents[1]


def _make_email(*, sender_name: str, sender_email: str, subject: str, body: str) -> EmailMessage:
    return EmailMessage(
        uid="1",
        message_id="<id@example.com>",
        message_hash="a" * 64,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        received_at="2026-11-24T11:00:00+09:00",
        received_date="2026-11-24",
        body_text=body,
    )


class TestRuleFilter(unittest.TestCase):
    def setUp(self) -> None:
        rules = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
        rules["company_aliases"] = {"우아한형제들": "배달의민족"}
        schema = json.loads((ROOT / "config" / "notion_schema.json").read_text(encoding="utf-8"))
        schema["known_roles"] = ["프로덕트 매니저", "데이터 엔지니어"]
        self.rule_filter = RuleFilter(rules, schema)

    def test_excludes_job_recommendation_newsletter(self) -> None:
        email_msg = _make_email(
            sender_name="원티드",
            sender_email="noreply@wantedlab.com",
            subject="[원티드] 회원님을 위한 이번 주 추천 포지션 10선",
            body="관심 있을 만한 공고를 모아봤어요. 지금 확인하세요.",
        )
        ok, reason = self.rule_filter.is_candidate(email_msg)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("excluded:"))

    def test_accepts_ats_recruitment_process_email(self) -> None:
        email_msg = _make_email(
            sender_name="비바리퍼블리카 채용팀",
            sender_email="noreply@greetinghr.com",
            subject="[토스] 서류 전형 결과 및 1차 면접 일정 안내",
            body="안녕하세요. 데이터 엔지니어 포지션 서류 전형에 합격하여 1차 면접 일정을 조율하고자 합니다.",
        )
        ok, _ = self.rule_filter.is_candidate(email_msg)
        self.assertTrue(ok)

    def test_fallback_classifies_document_rejection(self) -> None:
        email_msg = _make_email(
            sender_name="당근 채용팀",
            sender_email="recruit@daangn.com",
            subject="[당근] 서류 전형 결과 안내",
            body="지원해주셔서 감사합니다. 아쉽게도 이번 서류 전형에서는 모시지 못하게 되었습니다.",
        )
        result = self.rule_filter.classify_fallback(email_msg)
        self.assertTrue(result.is_recruitment_process)
        self.assertEqual(result.company_name, "당근")
        self.assertEqual(result.final_stage, "서류")
        self.assertEqual(result.outcome, "FAIL")
        self.assertTrue(result.needs_review)

    def test_fallback_classifies_final_offer(self) -> None:
        email_msg = _make_email(
            sender_name="우아한형제들 인재영입팀",
            sender_email="talent@woowahan.com",
            subject="[우아한형제들] 최종 합격 및 처우 협의 안내",
            body="데이터 엔지니어 포지션 최종 합격을 축하드립니다. 처우 협의를 진행합니다.",
        )
        result = self.rule_filter.classify_fallback(email_msg)
        self.assertEqual(result.company_name, "배달의민족")
        self.assertEqual(result.role, "데이터 엔지니어")
        self.assertEqual(result.final_stage, "처우협의")
        self.assertEqual(result.outcome, "PASS")

    def test_fallback_classifies_practical_interview_as_first_interview(self) -> None:
        email_msg = _make_email(
            sender_name="뤼튼테크놀로지스",
            sender_email="wrtntechnologies@greetinghr.com",
            subject="[뤼튼테크놀로지스] 인터뷰 일정 안내",
            body="확정된 실무 인터뷰 일정 안내 드립니다. 일시 : 12월 1일 오후 5시 30분",
        )
        result = self.rule_filter.classify_fallback(email_msg)
        self.assertEqual(result.final_stage, "1차면접")
        self.assertEqual(result.outcome, "IN_PROGRESS")

    def test_offer_negotiation_passes_filter_and_classifies_as_pass(self) -> None:
        email_msg = _make_email(
            sender_name="토스 인재영입팀",
            sender_email="people@toss.im",
            subject="[토스] 처우 협의 및 입사 제안 안내",
            body="최종 면접 전형을 통과하셨습니다. 연봉 협의 및 처우 안내를 위해 희망 입사일을 회신해 주세요.",
        )
        ok, _ = self.rule_filter.is_candidate(email_msg)
        self.assertTrue(ok)
        result = self.rule_filter.classify_fallback(email_msg)
        self.assertEqual(result.final_stage, "처우협의")
        self.assertEqual(result.outcome, "PASS")

    def test_non_dev_role_and_assignment_stage(self) -> None:
        email_msg = _make_email(
            sender_name="노바랩스 채용팀",
            sender_email="hr@novalabs.io",
            subject="[노바랩스] 프로덕트 매니저 사전 과제 안내",
            body="안녕하세요. 프로덕트 매니저 포지션 사전 과제 및 포트폴리오 제출 안내드립니다.",
        )
        ok, _ = self.rule_filter.is_candidate(email_msg)
        self.assertTrue(ok)
        result = self.rule_filter.classify_fallback(email_msg)
        self.assertEqual(result.role, "프로덕트 매니저")
        self.assertEqual(result.final_stage, "과제/테스트")
        self.assertEqual(result.outcome, "IN_PROGRESS")


if __name__ == "__main__":
    unittest.main()
