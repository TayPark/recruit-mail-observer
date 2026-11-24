"""Stage 2 LLM classifier supporting Gemini 4 Hydrogen Low and OpenAI."""

from __future__ import annotations

import json
import logging
import math
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from recruit_mail_observer.models import ClassificationResult, EmailMessage
from recruit_mail_observer.rule_filter import RuleFilter

LOGGER = logging.getLogger(__name__)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    fence = JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


class HybridClassifier:
    """Runs LLM classification with automatic rule-based fallback."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        thinking_level: str,
        min_confidence: float,
        notion_schema: dict[str, Any],
        rule_filter: RuleFilter,
        http_opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: int = 60,
        max_retries: int = 1,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.thinking_level = thinking_level
        self.min_confidence = min_confidence
        self.allowed_stages: list[str] = list(notion_schema.get("allowed_final_stages", []))
        self.known_roles: list[str] = list(notion_schema.get("known_roles", []))
        self.rule_filter = rule_filter
        self._http_opener = http_opener
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)

    def update_dynamic_options(
        self,
        *,
        roles: list[str] | None = None,
        stages: list[str] | None = None,
    ) -> None:
        if roles:
            merged = list(dict.fromkeys([*self.known_roles, *roles]))
            self.known_roles = merged
        if stages:
            self.allowed_stages = list(stages)

    def classify(self, email_msg: EmailMessage) -> ClassificationResult:
        if self.provider == "none" or not self.api_key:
            return self.rule_filter.classify_fallback(email_msg)

        prompt = self._build_prompt(email_msg)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.provider == "gemini":
                    raw_text = self._call_gemini(prompt)
                elif self.provider == "openai":
                    raw_text = self._call_openai(prompt)
                else:
                    raise ValueError(f"Unsupported LLM_PROVIDER: {self.provider}")
                return self._parse_llm_response(raw_text)
            except Exception as exc:  # pragma: no cover - verified via retry/fallback tests
                last_exc = exc
                if attempt < self.max_retries:
                    LOGGER.warning("LLM attempt %d failed (%s); retrying once.", attempt + 1, exc)
                    continue
        LOGGER.warning("LLM classification failed (%s); falling back to rules.", last_exc)
        return self.rule_filter.classify_fallback(email_msg)

    def _build_prompt(self, email_msg: EmailMessage) -> str:
        allowed_stages_json = json.dumps(self.allowed_stages, ensure_ascii=False)
        role_hint = (
            f"기존 직무 목록({json.dumps(self.known_roles, ensure_ascii=False)})과 의미가 일치하면 그 표기를 우선 사용하고, "
            if self.known_roles
            else ""
        )
        body_snippet = email_msg.body_text[:3000]

        return (
            "너는 모든 직군(개발, PM, 디자인, 마케팅, 영업, 경영지원 등)을 지원하는 한국 채용 프로세스 메일 분류기다. 아래 이메일을 분석해 반드시 JSON 객체만 반환하라.\n"
            "규칙:\n"
            "1. 단순 채용공고 추천, 헤드헌팅 스팸, 플랫폼 뉴스레터는 is_recruitment_process=false로 분류한다.\n"
            "2. 사용자가 실제로 지원했거나 전형을 진행 중인 회사(서류접수, 커피챗, 스크리닝, 과제/테스트, 면접, 합격, 불합격, 채용종료 등)만 is_recruitment_process=true다.\n"
            f"3. final_stage는 반드시 다음 목록 중 정확히 하나만 선택한다: {allowed_stages_json}\n"
            "   - 서류 접수/탈락은 '서류', 코딩테스트·사전과제·포트폴리오 과제·인적성·필기시험은 과제/테스트 계열, 실무/직무/1차 면접은 '1차면접', 임원/최종/2차 면접은 '2차면접', 최종 합격·오퍼·처우 협의는 '처우협의'를 선택한다.\n"
            "4. outcome은 'PASS'(최종합격/오퍼/처우협의), 'FAIL'(불합격/탈락), 'IN_PROGRESS'(진행중/접수/다음전형안내/일정조율), 'CLOSED'(채용종료/공고마감) 중 하나다.\n"
            "   - 주의: 서류 합격 후 1차면접 안내 메일은 아직 전형이 진행 중이므로 final_stage='1차면접', outcome='IN_PROGRESS'다.\n"
            f"5. role은 메일에 명시된 지원 직무명을 추출한다. {role_hint}불명확하면 빈 문자열 \"\".\n"
            "6. next_deadline은 메일에 명시된 면접 일시 또는 과제/서류 제출 마감일이 있으면 'YYYY-MM-DD' 형식으로 추출하고, 없으면 빈 문자열 \"\".\n"
            "7. summary는 한국어 1~2문장(120자 이내)으로 핵심 결과와 다음 일정/액션을 요약한다.\n"
            "8. confidence는 0.0~1.0 사이 실수다.\n\n"
            "출력 JSON 스키마:\n"
            '{"is_recruitment_process": true, "company_name": "회사명", "role": "직무명 또는 빈문자열", '
            '"final_stage": "허용목록중하나", "outcome": "PASS|FAIL|IN_PROGRESS|CLOSED", '
            '"next_deadline": "YYYY-MM-DD 또는 빈문자열", "summary": "한두줄 요약", "confidence": 0.92, "reason": "판단근거"}\n\n'
            f"발신자: {email_msg.sender_name} <{email_msg.sender_email}>\n"
            f"수신일시: {email_msg.received_at}\n"
            f"제목: {email_msg.subject}\n"
            f"본문:\n{body_snippet}"
        )

    def _call_gemini(self, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
            },
        }
        if self.thinking_level:
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingLevel": self.thinking_level,
            }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with self._http_opener(req, timeout=self.timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        candidates = body.get("candidates") or []
        if not candidates:
            raise ValueError("Gemini API returned no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if not text_parts:
            raise ValueError("Gemini API returned empty content parts")
        return "\n".join(text_parts)

    def _call_openai(self, prompt: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return only valid JSON matching the requested schema."},
                {"role": "user", "content": prompt},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with self._http_opener(req, timeout=self.timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        choices = body.get("choices") or []
        if not choices:
            raise ValueError("OpenAI API returned no choices")
        return str(choices[0]["message"]["content"])

    def _parse_llm_response(self, raw_text: str) -> ClassificationResult:
        data = _extract_json_object(raw_text)
        raw_is_rec = data.get("is_recruitment_process")
        if not isinstance(raw_is_rec, bool):
            raise ValueError("LLM field 'is_recruitment_process' must be a JSON boolean")
        is_recruitment = raw_is_rec

        raw_conf = data.get("confidence")
        if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)) or not math.isfinite(float(raw_conf)):
            raise ValueError("LLM field 'confidence' must be a finite number")
        confidence = max(0.0, min(1.0, float(raw_conf)))

        company_name = str(data.get("company_name") or "").strip() or "미확인 회사"
        role = str(data.get("role") or "").strip()
        final_stage = str(data.get("final_stage") or "").strip()
        outcome = str(data.get("outcome") or "IN_PROGRESS").strip().upper()
        summary = str(data.get("summary") or "").strip()
        reason = str(data.get("reason") or "").strip()
        raw_deadline = str(data.get("next_deadline") or "").strip()
        next_deadline = ""
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_deadline[:10]):
            try:
                datetime.strptime(raw_deadline[:10], "%Y-%m-%d")
                next_deadline = raw_deadline[:10]
            except ValueError:
                next_deadline = ""

        needs_review = confidence < self.min_confidence
        if outcome not in {"PASS", "FAIL", "IN_PROGRESS", "CLOSED"}:
            outcome = "IN_PROGRESS"
            needs_review = True

        if is_recruitment and final_stage not in self.allowed_stages:
            final_stage = "서류"
            needs_review = True

        return ClassificationResult(
            is_recruitment_process=is_recruitment,
            company_name=company_name,
            role=role,
            final_stage=final_stage or "서류",
            outcome=outcome,
            summary=summary,
            confidence=confidence,
            source="llm",
            needs_review=needs_review,
            reason=reason,
            next_deadline=next_deadline,
        )
