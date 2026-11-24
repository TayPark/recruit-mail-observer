"""Tests for AppConfig provider selection, bundled defaults, and CLI sanitized output."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from recruit_mail_observer.cli import main as cli_main
from recruit_mail_observer.config import AppConfig
from recruit_mail_observer.models import ClassificationResult, EmailMessage, RunReport


class TestConfigAndCli(unittest.TestCase):
    def test_gmail_and_naver_do_not_inherit_legacy_kakao_host(self) -> None:
        env = {
            "MAIL_PROVIDER": "gmail",
            "MAIL_EMAIL": "user@gmail.com",
            "MAIL_APP_PASSWORD": "pw",
            "KAKAO_IMAP_HOST": "imap.kakao.com",
            "KAKAO_IMAP_PORT": "993",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.from_env(config_dir=None, env_file=None)
        self.assertEqual(cfg.kakao_imap_host, "imap.gmail.com")
        self.assertEqual(cfg.kakao_imap_port, 993)

        env["MAIL_PROVIDER"] = "naver"
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.from_env(config_dir=None, env_file=None)
        self.assertEqual(cfg.kakao_imap_host, "imap.naver.com")

    def test_rejects_unknown_mail_provider_and_requires_host_for_custom(self) -> None:
        with mock.patch.dict(os.environ, {"MAIL_PROVIDER": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                AppConfig.from_env(config_dir=None, env_file=None)

        env = {"MAIL_PROVIDER": "custom", "MAIL_EMAIL": "a@b.com", "MAIL_APP_PASSWORD": "pw"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.from_env(config_dir=None, env_file=None)
            with self.assertRaises(ValueError):
                cfg.validate(dry_run=True)

    def test_selects_llm_api_key_strictly_by_provider(self) -> None:
        env = {
            "LLM_PROVIDER": "openai",
            "GEMINI_API_KEY": "gemini-secret",
            "OPENAI_API_KEY": "openai-secret",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.from_env(config_dir=None, env_file=None)
        self.assertEqual(cfg.llm_api_key, "openai-secret")
        self.assertEqual(cfg.llm_model, "gpt-4o-mini")

    def test_loads_bundled_defaults_from_unrelated_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with mock.patch.dict(os.environ, {}, clear=True):
                    cfg = AppConfig.from_env(config_dir=None, env_file=None)
                self.assertIn("ats_domains", cfg.rules)
                self.assertIn("allowed_final_stages", cfg.notion_schema)
                self.assertEqual(cfg.llm_provider, "none")
            finally:
                os.chdir(old_cwd)

    def test_cli_hides_event_details_unless_show_details_passed(self) -> None:
        sample_email = EmailMessage(
            uid="1",
            message_id="<1@test.com>",
            message_hash="a" * 64,
            sender_name="Nova",
            sender_email="hr@nova.io",
            subject="Sensitive Interview Subject",
            received_at="2026-11-24T10:00:00+09:00",
            received_date="2026-11-24",
            body_text="body",
        )
        sample_cls = ClassificationResult(
            is_recruitment_process=True,
            company_name="Nova",
            role="PM",
            final_stage="1차면접",
            outcome="IN_PROGRESS",
            summary="Confidential summary",
            confidence=0.95,
            source="llm",
        )
        fake_report = RunReport(scanned_unread=1, recruitment_detected=1)
        env = {"MAIL_EMAIL": "a@kakao.com", "MAIL_APP_PASSWORD": "pw"}

        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "recruit_mail_observer.cli.RecruitMailPipeline.run",
            return_value=(fake_report, [(sample_email, sample_cls, None)]),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--dry-run"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertNotIn("events", out)
            self.assertNotIn("Sensitive Interview Subject", buf.getvalue())

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc2 = cli_main(["--dry-run", "--show-details"])
            self.assertEqual(rc2, 0)
            out2 = json.loads(buf2.getvalue())
            self.assertEqual(len(out2["events"]), 1)
            self.assertEqual(out2["events"][0]["subject"], "Sensitive Interview Subject")


if __name__ == "__main__":
    unittest.main()
