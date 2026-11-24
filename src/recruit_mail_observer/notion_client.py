"""Notion database client for syncing recruitment progress."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from recruit_mail_observer.models import ClassificationResult, EmailMessage, NotionSyncResult

LOGGER = logging.getLogger(__name__)
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
CORP_PREFIX_SUFFIX_RE = re.compile(
    r"(?:\(주\)|\(유\)|주식회사|유한회사|inc\.?|corp\.?|co\.?,?\s*ltd\.?|llc)",
    re.IGNORECASE,
)
NON_ALNUM_KO_RE = re.compile(r"[^0-9a-zA-Z가-힣]+")


def normalize_company_name(name: str, aliases: dict[str, str] | None = None) -> str:
    cleaned = name.strip()
    if aliases:
        for alias, canonical in aliases.items():
            if alias.strip().lower() == cleaned.lower():
                cleaned = canonical.strip()
                break
    cleaned = CORP_PREFIX_SUFFIX_RE.sub("", cleaned)
    cleaned = NON_ALNUM_KO_RE.sub("", cleaned)
    return cleaned.lower()


@dataclass(frozen=True)
class NotionRow:
    page_id: str
    page_url: str
    company_name: str
    status: str
    final_stage: str
    updated_at: str
    role: str


class NotionRecruitmentClient:
    """Syncs classified recruitment updates to the target Notion database."""

    def __init__(
        self,
        *,
        token: str,
        database_id: str,
        notion_schema: dict[str, Any],
        company_aliases: dict[str, str] | None = None,
        match_by_role: bool = False,
        http_opener: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: int = 30,
    ) -> None:
        self.token = token
        self.database_id = database_id
        self.schema = notion_schema
        self.props = notion_schema["properties"]
        self.terminal_statuses = set(notion_schema.get("terminal_statuses", ["합격", "불합격", "종료"]))
        self.outcome_to_status: dict[str, str] = notion_schema.get(
            "outcome_to_status",
            {"IN_PROGRESS": "진행중", "PASS": "합격", "FAIL": "불합격", "CLOSED": "종료"},
        )
        self.stage_order: dict[str, int] = notion_schema.get("stage_order", {})
        self.company_aliases = company_aliases or {}
        self.match_by_role = match_by_role
        self._http_opener = http_opener
        self.timeout_seconds = timeout_seconds
        self._cached_rows: Optional[list[NotionRow]] = None
        self._existing_properties: Optional[set[str]] = None

    def fetch_database_metadata(self) -> dict[str, list[str]]:
        """Validate required Notion DB properties/types and read dynamic select options."""
        data = self._request("GET", f"/databases/{self.database_id}")
        props = data.get("properties") or {}
        self._existing_properties = set(props.keys())

        required_keys = ("start_date", "updated_at", "company", "status", "final_stage", "role")
        for key in required_keys:
            prop_cfg = self.props[key]
            prop_name = prop_cfg["name"]
            expected_type = prop_cfg["type"]
            if prop_name not in props:
                raise ValueError(f"Notion database is missing required property '{prop_name}' ({expected_type})")
            actual_type = (props[prop_name] or {}).get("type")
            if actual_type and actual_type != expected_type:
                raise ValueError(
                    f"Notion property '{prop_name}' has type '{actual_type}', expected '{expected_type}'"
                )

        role_prop_name = self.props["role"]["name"]
        stage_prop_name = self.props["final_stage"]["name"]
        role_options = [
            str(opt.get("name", "")).strip()
            for opt in ((props.get(role_prop_name) or {}).get("select") or {}).get("options", [])
            if opt.get("name")
        ]
        stage_options = [
            str(opt.get("name", "")).strip()
            for opt in ((props.get(stage_prop_name) or {}).get("select") or {}).get("options", [])
            if opt.get("name")
        ]
        return {"roles": role_options, "stages": stage_options}

    def _has_property(self, prop_key: str) -> bool:
        prop_cfg = self.props.get(prop_key)
        if not prop_cfg:
            return False
        if self._existing_properties is None:
            return False
        return prop_cfg["name"] in self._existing_properties

    def _resolve_status(self, outcome: str) -> str:
        return self.outcome_to_status.get(outcome, "진행중")

    def sync(
        self,
        email_msg: EmailMessage,
        classification: ClassificationResult,
    ) -> NotionSyncResult:
        rows = self._list_rows()
        matched, is_ambiguous = self._find_matching_row(
            rows,
            classification.company_name,
            role=classification.role,
        )
        incoming_status = self._resolve_status(classification.outcome)

        if is_ambiguous:
            return NotionSyncResult(
                action="skipped",
                page_id=None,
                page_url=None,
                matched_company=classification.company_name,
                applied_stage=classification.final_stage,
                note="ambiguous_match",
            )

        if matched is not None:
            if (
                matched.status in self.terminal_statuses
                and incoming_status not in self.terminal_statuses
                and matched.updated_at
                and email_msg.received_date > matched.updated_at
                and classification.final_stage == "서류"
            ):
                return NotionSyncResult(
                    action="skipped",
                    page_id=matched.page_id,
                    page_url=matched.page_url,
                    matched_company=matched.company_name,
                    applied_stage=matched.final_stage,
                    note="possible_reapplication_review",
                )
            if self._is_stage_regression(matched, email_msg.received_date, incoming_status, classification.final_stage):
                return NotionSyncResult(
                    action="skipped",
                    page_id=matched.page_id,
                    page_url=matched.page_url,
                    matched_company=matched.company_name,
                    applied_stage=matched.final_stage,
                    note="skipped_stage_regression",
                )
            updated_row = self._update_row(matched, email_msg, classification, incoming_status)
            self._append_summary_block(updated_row.page_id, email_msg, classification, incoming_status)
            self._replace_cached_row(updated_row)
            return NotionSyncResult(
                action="updated",
                page_id=updated_row.page_id,
                page_url=updated_row.page_url,
                matched_company=updated_row.company_name,
                applied_stage=updated_row.final_stage,
            )

        created_row = self._create_row(email_msg, classification, incoming_status)
        self._append_summary_block(created_row.page_id, email_msg, classification, incoming_status)
        if self._cached_rows is not None:
            self._cached_rows.append(created_row)
        return NotionSyncResult(
            action="created",
            page_id=created_row.page_id,
            page_url=created_row.page_url,
            matched_company=created_row.company_name,
            applied_stage=created_row.final_stage,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            method=method,
        )
        with self._http_opener(req, timeout=self.timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _list_rows(self) -> list[NotionRow]:
        if self._cached_rows is not None:
            return self._cached_rows

        rows: list[NotionRow] = []
        next_cursor: Optional[str] = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if next_cursor:
                payload["start_cursor"] = next_cursor
            data = self._request("POST", f"/databases/{self.database_id}/query", payload)
            for item in data.get("results", []):
                rows.append(self._parse_page_row(item))
            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")

        self._cached_rows = rows
        return rows

    def _parse_page_row(self, page: dict[str, Any]) -> NotionRow:
        properties = page.get("properties", {})
        company_prop = properties.get(self.props["company"]["name"], {})
        title_items = company_prop.get("title") or []
        company_name = "".join(t.get("plain_text", "") for t in title_items).strip()

        status_prop = properties.get(self.props["status"]["name"], {})
        status_select = status_prop.get("select") or {}
        status = str(status_select.get("name") or "").strip()

        stage_prop = properties.get(self.props["final_stage"]["name"], {})
        stage_select = stage_prop.get("select") or {}
        final_stage = str(stage_select.get("name") or "").strip()

        updated_prop = properties.get(self.props["updated_at"]["name"], {})
        updated_date = updated_prop.get("date") or {}
        updated_at = str(updated_date.get("start") or "").strip()[:10]

        role_prop = properties.get(self.props["role"]["name"], {})
        role_select = role_prop.get("select") or {}
        role = str(role_select.get("name") or "").strip()

        return NotionRow(
            page_id=str(page.get("id") or ""),
            page_url=str(page.get("url") or ""),
            company_name=company_name,
            status=status,
            final_stage=final_stage,
            updated_at=updated_at,
            role=role,
        )

    def _find_matching_row(
        self,
        rows: list[NotionRow],
        company_name: str,
        *,
        role: str = "",
    ) -> tuple[Optional[NotionRow], bool]:
        """Match exact raw or normalized canonical/alias company name; flag ambiguous matches."""
        target_raw = company_name.strip().lower()
        target_norm = normalize_company_name(company_name, self.company_aliases)
        if not target_norm:
            return None, False

        candidates = [row for row in rows if row.company_name.strip().lower() == target_raw]
        if not candidates:
            candidates = [
                row
                for row in rows
                if normalize_company_name(row.company_name, self.company_aliases) == target_norm
            ]

        if not candidates:
            return None, False

        if not self.match_by_role:
            if len(candidates) == 1:
                return candidates[0], False
            active = [r for r in candidates if r.status not in self.terminal_statuses]
            if len(active) == 1:
                return active[0], False
            return None, True

        target_role = role.strip().lower()
        if not target_role:
            if len(candidates) == 1:
                return candidates[0], False
            return None, True

        role_matches = [row for row in candidates if row.role.strip().lower() == target_role]
        if len(role_matches) == 1:
            return role_matches[0], False
        if len(role_matches) > 1:
            return None, True

        empty_role_matches = [row for row in candidates if not row.role.strip()]
        if len(empty_role_matches) == 1:
            return empty_role_matches[0], False
        if len(empty_role_matches) > 1:
            return None, True
        return None, False

    def _is_stage_regression(
        self,
        row: NotionRow,
        incoming_date: str,
        incoming_status: str,
        incoming_stage: str,
    ) -> bool:
        if not row.final_stage and not row.status:
            return False
        if row.updated_at and incoming_date < row.updated_at:
            return True
        if row.status in self.terminal_statuses and incoming_status not in self.terminal_statuses:
            return True
        current_rank = self.stage_order.get(row.final_stage, 0)
        incoming_rank = self.stage_order.get(incoming_stage, 0)
        if current_rank > incoming_rank and incoming_status not in self.terminal_statuses:
            return True
        return False

    def _update_row(
        self,
        row: NotionRow,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        incoming_status: str,
    ) -> NotionRow:
        properties: dict[str, Any] = {
            self.props["updated_at"]["name"]: {"date": {"start": email_msg.received_date}},
            self.props["status"]["name"]: {"select": {"name": incoming_status}},
            self.props["final_stage"]["name"]: {"select": {"name": classification.final_stage}},
        }
        if classification.next_deadline and self._has_property("next_deadline"):
            properties[self.props["next_deadline"]["name"]] = {
                "date": {"start": classification.next_deadline}
            }
        new_role = row.role
        if not row.role and classification.role:
            properties[self.props["role"]["name"]] = {"select": {"name": classification.role}}
            new_role = classification.role

        data = self._request("PATCH", f"/pages/{row.page_id}", {"properties": properties})
        return NotionRow(
            page_id=row.page_id,
            page_url=str(data.get("url") or row.page_url),
            company_name=row.company_name,
            status=incoming_status,
            final_stage=classification.final_stage,
            updated_at=email_msg.received_date,
            role=new_role,
        )

    def _create_row(
        self,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        incoming_status: str,
    ) -> NotionRow:
        canonical_company = self.company_aliases.get(classification.company_name, classification.company_name)
        properties: dict[str, Any] = {
            self.props["start_date"]["name"]: {"date": {"start": email_msg.received_date}},
            self.props["updated_at"]["name"]: {"date": {"start": email_msg.received_date}},
            self.props["company"]["name"]: {
                "title": [{"text": {"content": canonical_company}}]
            },
            self.props["status"]["name"]: {"select": {"name": incoming_status}},
            self.props["final_stage"]["name"]: {
                "select": {"name": classification.final_stage}
            },
        }
        if classification.role:
            properties[self.props["role"]["name"]] = {"select": {"name": classification.role}}
        if classification.next_deadline and self._has_property("next_deadline"):
            properties[self.props["next_deadline"]["name"]] = {
                "date": {"start": classification.next_deadline}
            }

        data = self._request(
            "POST",
            "/pages",
            {
                "parent": {"database_id": self.database_id},
                "properties": properties,
            },
        )
        return NotionRow(
            page_id=str(data.get("id") or ""),
            page_url=str(data.get("url") or ""),
            company_name=canonical_company,
            status=incoming_status,
            final_stage=classification.final_stage,
            updated_at=email_msg.received_date,
            role=classification.role,
        )

    def _append_summary_block(
        self,
        page_id: str,
        email_msg: EmailMessage,
        classification: ClassificationResult,
        incoming_status: str,
    ) -> None:
        if not page_id:
            return
        line = (
            f"[{email_msg.received_date}] [{incoming_status}/{classification.final_stage}] "
            f"{classification.summary or email_msg.subject}"
        )
        payload = {
            "children": [
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line[:1800]}}]
                    },
                }
            ]
        }
        try:
            self._request("PATCH", f"/blocks/{page_id}/children", payload)
        except Exception as exc:  # pragma: no cover - best-effort log
            LOGGER.warning("Failed to append summary block to Notion page %s: %s", page_id, exc)

    def _replace_cached_row(self, updated: NotionRow) -> None:
        if self._cached_rows is None:
            return
        self._cached_rows = [
            updated if r.page_id == updated.page_id else r for r in self._cached_rows
        ]
