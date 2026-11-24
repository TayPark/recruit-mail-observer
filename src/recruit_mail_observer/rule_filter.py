"""Stage 1 rule filter and deterministic fallback classifier."""

from __future__ import annotations

import re
from typing import Any

from recruit_mail_observer.models import ClassificationResult, EmailMessage

BRACKET_COMPANY_RE = re.compile(r"^[\[\(【]\s*([^\]\)】]{2,30})\s*[\]\)】]")


class RuleFilter:
    """Filters obvious non-recruitment emails and provides a rule-based fallback classifier."""

    def __init__(self, rules: dict[str, Any], notion_schema: dict[str, Any]) -> None:
        self.ats_domains = tuple(d.lower() for d in rules.get("ats_domains", []))
        self.sender_hints = tuple(h.lower() for h in rules.get("sender_local_part_hints", []))
        self.exclude_patterns = tuple(p.lower() for p in rules.get("exclude_patterns", []))
        self.process_keywords = tuple(k.lower() for k in rules.get("process_keywords", []))
        self.strong_outcome_keywords = tuple(
            k.lower() for k in rules.get("strong_outcome_keywords", [])
        )
        self.company_aliases: dict[str, str] = rules.get("company_aliases", {})
        self.allowed_stages: list[str] = list(notion_schema.get("allowed_final_stages", []))
        self.known_roles: list[str] = list(notion_schema.get("known_roles", []))

    def update_dynamic_options(
        self,
        *,
        roles: list[str] | None = None,
        stages: list[str] | None = None,
    ) -> None:
        if roles:
            self.known_roles = list(dict.fromkeys([*self.known_roles, *roles]))
        if stages:
            self.allowed_stages = list(stages)

    def is_candidate(self, email_msg: EmailMessage) -> tuple[bool, str]:
        combined = f"{email_msg.subject}\n{email_msg.body_text[:2000]}".lower()
        sender_email = email_msg.sender_email.lower()
        local_part, _, domain = sender_email.partition("@")

        has_strong_outcome = any(k in combined for k in self.strong_outcome_keywords)
        matched_exclude = next((p for p in self.exclude_patterns if p in combined), None)
        if matched_exclude and not has_strong_outcome:
            return False, f"excluded:{matched_exclude}"

        if domain and any(domain == ats or domain.endswith(f".{ats}") for ats in self.ats_domains):
            if any(k in combined for k in self.process_keywords) or has_strong_outcome:
                return True, f"ats_domain:{domain}"
            # ATS senders often send pure newsletters too; require at least one process hint
            return False, f"ats_without_process_keyword:{domain}"

        if has_strong_outcome:
            return True, "strong_outcome_keyword"

        sender_is_hr = any(hint in local_part for hint in self.sender_hints)
        keyword_hits = [k for k in self.process_keywords if k in combined]
        if sender_is_hr and keyword_hits:
            return True, f"hr_sender+keyword:{keyword_hits[0]}"
        if len(keyword_hits) >= 2:
            return True, f"multi_keywords:{','.join(keyword_hits[:2])}"

        return False, "no_recruitment_signal"

    def classify_fallback(self, email_msg: EmailMessage) -> ClassificationResult:
        text = f"{email_msg.subject}\n{email_msg.body_text[:3000]}"
        lower = text.lower()

        final_stage, outcome = self._infer_stage_and_outcome(lower)
        company_name = self._infer_company_name(email_msg)
        role = self._infer_role(text)
        summary = f"[규칙 분류] {email_msg.subject[:90]}"

        return ClassificationResult(
            is_recruitment_process=True,
            company_name=company_name,
            role=role,
            final_stage=final_stage,
            outcome=outcome,
            summary=summary,
            confidence=0.55,
            source="rule_fallback",
            needs_review=True,
            reason="classified_by_rule_fallback",
        )

    def _infer_stage_and_outcome(self, lower: str) -> tuple[str, str]:
        fail_markers = (
            "불합격",
            "모시지 못하게",
            "함께하기 어렵",
            "함께하지 못하게",
            "다음 기회에",
            "아쉽게도",
            "regret to inform",
            "not moving forward",
            "unfortunately",
        )
        is_fail = any(m in lower for m in fail_markers)

        if is_fail:
            if any(k in lower for k in ("2차", "최종 면접", "최종 인터뷰", "임원")):
                return "2차면접", "FAIL"
            if any(k in lower for k in ("1차", "실무 면접", "실무 인터뷰", "직무 면접", "역량 면접", "면접", "인터뷰")):
                return "1차면접", "FAIL"
            if any(
                k in lower
                for k in ("코딩테스트", "코딩 테스트", "코테", "과제", "take-home", "인성검사", "인적성", "필기시험", "적성검사", "포트폴리오")
            ):
                return self._test_stage_name(), "FAIL"
            if "스크리닝" in lower:
                return "스크리닝", "FAIL"
            return "서류", "FAIL"

        if "채용 종료" in lower or "채용이 종료" in lower or "포지션이 마감" in lower:
            return "서류", "CLOSED"

        if any(
            k in lower
            for k in (
                "최종 합격",
                "최종합격",
                "입사를 축하",
                "오퍼 레터",
                "offer letter",
                "처우 협의",
                "처우협의",
                "처우 안내",
                "처우 제안",
                "연봉 협의",
                "연봉 협상",
                "입사 제안",
            )
        ):
            return "처우협의", "PASS"
        if any(k in lower for k in ("2차 면접", "2차면접", "2차 인터뷰", "최종 면접", "최종 인터뷰", "임원 면접", "임원 인터뷰")):
            return "2차면접", "IN_PROGRESS"
        if any(
            k in lower
            for k in (
                "1차 면접",
                "1차면접",
                "1차 인터뷰",
                "실무 면접",
                "실무 인터뷰",
                "직무 면접",
                "역량 면접",
                "기술 면접",
                "기술 인터뷰",
                "케이스 인터뷰",
                "interview invitation",
                "schedule an interview",
                "technical interview",
                "interview",
            )
        ):
            return "1차면접", "IN_PROGRESS"
        if any(
            k in lower
            for k in ("코딩테스트", "코딩 테스트", "coding test", "과제", "take-home", "case study", "인성검사", "인적성", "필기시험", "적성검사")
        ):
            return self._test_stage_name(), "IN_PROGRESS"
        if "스크리닝" in lower or "screening" in lower:
            return "스크리닝", "IN_PROGRESS"
        if "커피챗" in lower or "coffee chat" in lower:
            return "커피챗", "IN_PROGRESS"

        return "서류", "IN_PROGRESS"

    def _test_stage_name(self) -> str:
        if "과제/테스트" in self.allowed_stages:
            return "과제/테스트"
        if "코테/과제" in self.allowed_stages:
            return "코테/과제"
        return "과제/테스트"

    def _infer_company_name(self, email_msg: EmailMessage) -> str:
        bracket_match = BRACKET_COMPANY_RE.search(email_msg.subject.strip())
        if bracket_match:
            candidate = bracket_match.group(1).strip()
            return self._apply_alias(candidate)

        if email_msg.sender_name:
            cleaned = re.sub(r"(채용팀|인재영입팀|인사팀|HR|Recruiting|Talent).*$", "", email_msg.sender_name, flags=re.I).strip(" -|[]()")
            if cleaned and len(cleaned) >= 2:
                return self._apply_alias(cleaned)

        _, _, domain = email_msg.sender_email.partition("@")
        if domain:
            root = domain.split(".")[0]
            if root and root not in {"gmail", "naver", "kakao", "daum", "greetinghr", "ninehire"}:
                return self._apply_alias(root)
        return "미확인 회사"

    def _infer_role(self, text: str) -> str:
        lower = text.lower()
        for role in self.known_roles:
            if role.lower() in lower:
                return role
        return ""

    def _apply_alias(self, name: str) -> str:
        for alias, canonical in self.company_aliases.items():
            if alias.lower() == name.lower():
                return canonical
        return name
