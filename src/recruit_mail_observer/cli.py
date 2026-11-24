"""Command-line interface for recruit-mail-observer."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

from recruit_mail_observer.config import AppConfig
from recruit_mail_observer.imap_client import KakaoImapClient
from recruit_mail_observer.llm_classifier import HybridClassifier
from recruit_mail_observer.notion_client import NotionRecruitmentClient
from recruit_mail_observer.pipeline import RecruitMailPipeline
from recruit_mail_observer.rule_filter import RuleFilter
from recruit_mail_observer.slack_notifier import SlackNotifier
from recruit_mail_observer.state_store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recruit-mail-observer",
        description="Observe unread recruitment emails via IMAP, notify Slack, and sync Notion.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and classify unread emails without modifying Notion, Slack, or state file.",
    )
    parser.add_argument(
        "--show-details",
        action="store_true",
        help="Include per-message details in stdout JSON (for trusted local debugging only).",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Override LOOKBACK_DAYS for IMAP UNSEEN search.",
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=None,
        help="Maximum number of most-recent unread emails to inspect.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Optional directory containing custom rules.json and notion_schema.json.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Optional local .env file path.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Override STATE_FILE path for processed message hashes.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env(
        config_dir=args.config_dir,
        env_file=args.env_file,
        state_file_override=args.state_file,
        lookback_days_override=args.since_days,
    )
    config.validate(dry_run=args.dry_run)

    imap_client = KakaoImapClient(
        host=config.kakao_imap_host,
        port=config.kakao_imap_port,
        email_address=config.kakao_email,
        app_password=config.kakao_app_password,
        mailbox=config.kakao_imap_mailbox,
    )
    state_store = StateStore(config.state_file)
    rule_filter = RuleFilter(config.rules, config.notion_schema)
    classifier = HybridClassifier(
        provider=config.llm_provider,
        api_key=config.llm_api_key,
        model=config.llm_model,
        thinking_level=config.gemini_thinking_level,
        min_confidence=config.min_confidence,
        notion_schema=config.notion_schema,
        rule_filter=rule_filter,
    )

    notion_client = (
        None
        if args.dry_run
        else NotionRecruitmentClient(
            token=config.notion_token,
            database_id=config.notion_database_id,
            notion_schema=config.notion_schema,
            company_aliases=config.rules.get("company_aliases", {}),
            match_by_role=config.match_by_role,
        )
    )
    slack_notifier = (
        None
        if args.dry_run
        else SlackNotifier(
            webhook_url=config.slack_webhook_url,
            mail_provider=config.mail_provider,
        )
    )

    pipeline = RecruitMailPipeline(
        imap_client=imap_client,
        state_store=state_store,
        rule_filter=rule_filter,
        classifier=classifier,
        notion_client=notion_client,
        slack_notifier=slack_notifier,
    )
    report, events = pipeline.run(
        lookback_days=config.lookback_days,
        max_emails=args.max_emails,
        dry_run=args.dry_run,
    )

    summary: dict[str, object] = {
        "dry_run": args.dry_run,
        "scanned_unread": report.scanned_unread,
        "skipped_already_processed": report.skipped_already_processed,
        "filtered_non_candidate": report.filtered_non_candidate,
        "classified_non_recruitment": report.classified_non_recruitment,
        "recruitment_detected": report.recruitment_detected,
        "notion_updated": report.notion_updated,
        "notion_created": report.notion_created,
        "notion_skipped": report.notion_skipped,
        "slack_notified": report.slack_notified,
        "error_count": len(report.errors),
        "errors": report.errors,
    }
    if args.show_details:
        summary["events"] = [
            {
                "received_date": email_msg.received_date,
                "subject": email_msg.subject,
                "company_name": cls.company_name,
                "role": cls.role,
                "final_stage": cls.final_stage,
                "outcome": cls.outcome,
                "next_deadline": cls.next_deadline,
                "confidence": cls.confidence,
                "source": cls.source,
                "needs_review": cls.needs_review,
                "summary": cls.summary,
                "notion_action": notion.action if notion else "dry_run",
            }
            for email_msg, cls, notion in events
        ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
