"""Domain data models for recruit-mail-observer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EmailMessage:
    uid: str
    message_id: str
    message_hash: str
    sender_name: str
    sender_email: str
    subject: str
    received_at: str  # ISO 8601 string
    received_date: str  # YYYY-MM-DD
    body_text: str


@dataclass(frozen=True)
class ClassificationResult:
    is_recruitment_process: bool
    company_name: str
    role: str
    final_stage: str
    outcome: str  # PASS | FAIL | IN_PROGRESS | CLOSED
    summary: str
    confidence: float
    source: str  # llm | rule_fallback
    needs_review: bool = False
    reason: str = ""
    next_deadline: str = ""  # YYYY-MM-DD if an interview/test deadline is mentioned


@dataclass(frozen=True)
class NotionSyncResult:
    action: str  # updated | created | skipped
    page_id: Optional[str]
    page_url: Optional[str]
    matched_company: str
    applied_stage: str
    note: str = ""


@dataclass
class RunReport:
    scanned_unread: int = 0
    skipped_already_processed: int = 0
    filtered_non_candidate: int = 0
    classified_non_recruitment: int = 0
    recruitment_detected: int = 0
    notion_updated: int = 0
    notion_created: int = 0
    notion_skipped: int = 0
    slack_notified: int = 0
    errors: list[str] = field(default_factory=list)
