"""End-to-end recruitment mail observation pipeline with review guard and per-step resume."""

from __future__ import annotations

import logging
from typing import Optional

from recruit_mail_observer.imap_client import KakaoImapClient
from recruit_mail_observer.llm_classifier import HybridClassifier
from recruit_mail_observer.models import ClassificationResult, EmailMessage, NotionSyncResult, RunReport
from recruit_mail_observer.notion_client import NotionRecruitmentClient
from recruit_mail_observer.rule_filter import RuleFilter
from recruit_mail_observer.slack_notifier import SlackNotifier
from recruit_mail_observer.state_store import StateStore

LOGGER = logging.getLogger(__name__)


class RecruitMailPipeline:
    """Coordinates IMAP fetch, dedup, hybrid classification, Notion sync, and Slack notify."""

    def __init__(
        self,
        *,
        imap_client: KakaoImapClient,
        state_store: StateStore,
        rule_filter: RuleFilter,
        classifier: HybridClassifier,
        notion_client: Optional[NotionRecruitmentClient],
        slack_notifier: Optional[SlackNotifier],
    ) -> None:
        self.imap_client = imap_client
        self.state_store = state_store
        self.rule_filter = rule_filter
        self.classifier = classifier
        self.notion_client = notion_client
        self.slack_notifier = slack_notifier

    def run(
        self,
        *,
        lookback_days: int = 7,
        max_emails: Optional[int] = None,
        dry_run: bool = False,
    ) -> tuple[RunReport, list[tuple[EmailMessage, ClassificationResult, Optional[NotionSyncResult]]]]:
        self.state_store.load()
        report = RunReport()
        processed_events: list[tuple[EmailMessage, ClassificationResult, Optional[NotionSyncResult]]] = []

        if self.notion_client is not None and hasattr(self.notion_client, "fetch_database_metadata"):
            try:
                meta = self.notion_client.fetch_database_metadata()
                roles = meta.get("roles")
                stages = meta.get("stages")
                if hasattr(self.classifier, "update_dynamic_options"):
                    self.classifier.update_dynamic_options(roles=roles, stages=stages)
                if hasattr(self.rule_filter, "update_dynamic_options"):
                    self.rule_filter.update_dynamic_options(roles=roles, stages=stages)
            except Exception as exc:  # pragma: no cover - best-effort dynamic metadata
                LOGGER.warning("Could not load dynamic Notion DB metadata (%s)", type(exc).__name__)

        emails = self.imap_client.fetch_unread_since(
            lookback_days=lookback_days,
            max_emails=max_emails,
        )
        fetch_errors = getattr(self.imap_client, "last_fetch_errors", None) or []
        report.errors.extend(fetch_errors)
        report.scanned_unread = len(emails)

        try:
            for email_msg in emails:
                if self.state_store.contains(email_msg.message_hash):
                    report.skipped_already_processed += 1
                    continue

                prior = self.state_store.get_record(email_msg.message_hash)
                is_partial_resume = bool(prior and prior.get("notion_done"))

                if not is_partial_resume:
                    is_candidate, filter_reason = self.rule_filter.is_candidate(email_msg)
                    if not is_candidate:
                        report.filtered_non_candidate += 1
                        if not dry_run:
                            self.state_store.mark_processed(
                                email_msg.message_hash,
                                status=f"filtered:{filter_reason}",
                            )
                        continue

                classification = self.classifier.classify(email_msg)
                if not classification.is_recruitment_process and not is_partial_resume:
                    report.classified_non_recruitment += 1
                    if not dry_run:
                        self.state_store.mark_processed(email_msg.message_hash, status="non_recruitment")
                    continue

                report.recruitment_detected += 1
                if dry_run:
                    processed_events.append((email_msg, classification, None))
                    continue

                try:
                    if self.notion_client is None or self.slack_notifier is None:
                        raise RuntimeError("Notion and Slack clients are required when dry_run=False")

                    if is_partial_resume and prior is not None:
                        notion_result = NotionSyncResult(
                            action=str(prior.get("notion_action") or "skipped"),
                            page_id=prior.get("notion_page_id"),
                            page_url=prior.get("notion_page_url"),
                            matched_company=str(prior.get("matched_company") or classification.company_name),
                            applied_stage=str(prior.get("applied_stage") or classification.final_stage),
                            note="resumed_after_notion",
                        )
                    elif classification.needs_review:
                        notion_result = NotionSyncResult(
                            action="skipped",
                            page_id=None,
                            page_url=None,
                            matched_company=classification.company_name,
                            applied_stage=classification.final_stage,
                            note="needs_review",
                        )
                        report.notion_skipped += 1
                    else:
                        notion_result = self.notion_client.sync(email_msg, classification)
                        if notion_result.action == "updated":
                            report.notion_updated += 1
                        elif notion_result.action == "created":
                            report.notion_created += 1
                        else:
                            report.notion_skipped += 1
                        self.state_store.mark_notion_done(
                            email_msg.message_hash,
                            notion_action=notion_result.action,
                            notion_page_id=notion_result.page_id,
                            notion_page_url=notion_result.page_url,
                            matched_company=notion_result.matched_company,
                            applied_stage=notion_result.applied_stage,
                        )

                    self.slack_notifier.notify(email_msg, classification, notion_result)
                    report.slack_notified += 1
                    self.state_store.mark_processed(
                        email_msg.message_hash,
                        status=f"notified:{notion_result.action}",
                    )
                    processed_events.append((email_msg, classification, notion_result))
                except Exception as exc:
                    LOGGER.warning("Failed processing email uid=%s (%s)", email_msg.uid, type(exc).__name__)
                    report.errors.append(f"uid={email_msg.uid}:{type(exc).__name__}")
        finally:
            if not dry_run:
                self.state_store.save()

        return report, processed_events
