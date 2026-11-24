"""Privacy-safe processed message state store with per-step resume support."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

KST = timezone(timedelta(hours=9))


class StateStore:
    """Tracks processed email Message-ID hashes and per-step completion to avoid replays."""

    def __init__(self, path: Path, *, max_entries: int = 5000, retention_days: int = 90) -> None:
        self.path = path
        self.max_entries = max_entries
        self.retention_days = retention_days
        self._records: dict[str, dict[str, Any]] = {}
        self._dirty = False

    def load(self) -> None:
        if not self.path.is_file():
            self._records = {}
            self._dirty = False
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        records = data.get("processed", {})
        self._records = records if isinstance(records, dict) else {}
        self._dirty = False

    def get_record(self, message_hash: str) -> Optional[dict[str, Any]]:
        record = self._records.get(message_hash)
        return dict(record) if isinstance(record, dict) else None

    def contains(self, message_hash: str) -> bool:
        """Return True only when a message has fully completed processing."""
        record = self._records.get(message_hash)
        if not isinstance(record, dict):
            return False
        if record.get("status") == "partial:notion_done":
            return False
        return True

    def mark_notion_done(
        self,
        message_hash: str,
        *,
        notion_action: str,
        notion_page_id: Optional[str],
        notion_page_url: Optional[str],
        matched_company: str,
        applied_stage: str,
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(KST)).isoformat(timespec="seconds")
        self._records[message_hash] = {
            "processed_at": timestamp,
            "status": "partial:notion_done",
            "notion_done": True,
            "notion_action": notion_action,
            "notion_page_id": notion_page_id,
            "notion_page_url": notion_page_url,
            "matched_company": matched_company,
            "applied_stage": applied_stage,
            "slack_done": False,
        }
        self._dirty = True

    def mark_processed(self, message_hash: str, *, status: str, now: datetime | None = None) -> None:
        timestamp = (now or datetime.now(KST)).isoformat(timespec="seconds")
        self._records[message_hash] = {
            "processed_at": timestamp,
            "status": status,
        }
        self._dirty = True

    def prune(self, *, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(KST)) - timedelta(days=self.retention_days)
        kept: list[tuple[str, dict[str, Any]]] = []
        for key, record in self._records.items():
            ts = record.get("processed_at", "")
            try:
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=KST)
                if parsed < cutoff:
                    self._dirty = True
                    continue
            except ValueError:
                pass
            kept.append((key, record))

        if len(kept) > self.max_entries:
            kept.sort(key=lambda item: item[1].get("processed_at", ""))
            kept = kept[-self.max_entries :]
            self._dirty = True

        self._records = dict(kept)

    def save(self) -> bool:
        if not self._dirty and self.path.is_file():
            return False
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "processed": self._records,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._dirty = False
        return True
