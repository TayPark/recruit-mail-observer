"""Configuration and environment loading with multi-provider IMAP presets and bundled defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

MAIL_PROVIDER_PRESETS: dict[str, tuple[str, int]] = {
    "kakao": ("imap.kakao.com", 993),
    "gmail": ("imap.gmail.com", 993),
    "naver": ("imap.naver.com", 993),
}
SUPPORTED_MAIL_PROVIDERS = set(MAIL_PROVIDER_PRESETS.keys()) | {"custom"}
SUPPORTED_LLM_PROVIDERS = {"none", "gemini", "openai"}


def load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a local .env file without overriding existing env vars."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _load_json_config(filename: str, config_dir: Path | None) -> dict[str, Any]:
    if config_dir is not None:
        candidate = config_dir / filename
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
        # If user explicitly specified a non-default config dir that is missing the file, fail clearly.
        if config_dir != Path("config"):
            raise FileNotFoundError(f"Config file not found: {candidate}")
    default_file = resources.files("recruit_mail_observer").joinpath("defaults", filename)
    return json.loads(default_file.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AppConfig:
    mail_provider: str
    kakao_email: str
    kakao_app_password: str
    kakao_imap_host: str
    kakao_imap_port: int
    kakao_imap_mailbox: str
    slack_webhook_url: str
    notion_token: str
    notion_database_id: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    gemini_thinking_level: str
    lookback_days: int
    min_confidence: float
    match_by_role: bool
    state_file: Path
    rules: dict[str, Any]
    notion_schema: dict[str, Any]

    @classmethod
    def from_env(
        cls,
        *,
        config_dir: Path | None = None,
        env_file: Path | None = None,
        state_file_override: Path | None = None,
        lookback_days_override: int | None = None,
    ) -> "AppConfig":
        if env_file is not None:
            load_dotenv(env_file)

        rules = _load_json_config("rules.json", config_dir)
        notion_schema = _load_json_config("notion_schema.json", config_dir)

        mail_provider = os.environ.get("MAIL_PROVIDER", "kakao").strip().lower() or "kakao"
        if mail_provider not in SUPPORTED_MAIL_PROVIDERS:
            raise ValueError(
                f"Unsupported MAIL_PROVIDER '{mail_provider}'. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_MAIL_PROVIDERS))}"
            )

        is_kakao = mail_provider == "kakao"
        preset_host, preset_port = MAIL_PROVIDER_PRESETS.get(mail_provider, ("", 993))

        mail_email = os.environ.get("MAIL_EMAIL", "").strip()
        if not mail_email and is_kakao:
            mail_email = os.environ.get("KAKAO_EMAIL", "").strip()

        mail_password = os.environ.get("MAIL_APP_PASSWORD", "").strip()
        if not mail_password and is_kakao:
            mail_password = os.environ.get("KAKAO_APP_PASSWORD", "").strip()

        mail_host = os.environ.get("MAIL_IMAP_HOST", "").strip()
        if not mail_host and is_kakao:
            mail_host = os.environ.get("KAKAO_IMAP_HOST", "").strip()
        if not mail_host:
            mail_host = preset_host

        mail_port_raw = os.environ.get("MAIL_IMAP_PORT", "").strip()
        if not mail_port_raw and is_kakao:
            mail_port_raw = os.environ.get("KAKAO_IMAP_PORT", "").strip()
        mail_port = int(mail_port_raw) if mail_port_raw else preset_port

        mail_mailbox = os.environ.get("MAIL_IMAP_MAILBOX", "").strip()
        if not mail_mailbox and is_kakao:
            mail_mailbox = os.environ.get("KAKAO_IMAP_MAILBOX", "").strip()
        if not mail_mailbox:
            mail_mailbox = "INBOX"

        provider = os.environ.get("LLM_PROVIDER", "none").strip().lower() or "none"
        if provider not in SUPPORTED_LLM_PROVIDERS:
            raise ValueError(
                f"Unsupported LLM_PROVIDER '{provider}'. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}"
            )

        if provider == "gemini":
            api_key = (
                os.environ.get("GEMINI_API_KEY", "").strip()
                or os.environ.get("LLM_API_KEY", "").strip()
            )
            default_model = "gemini-4-hydrogen"
        elif provider == "openai":
            api_key = (
                os.environ.get("OPENAI_API_KEY", "").strip()
                or os.environ.get("LLM_API_KEY", "").strip()
            )
            default_model = "gpt-4o-mini"
        else:
            api_key = ""
            default_model = ""

        llm_model = os.environ.get("LLM_MODEL", "").strip() or default_model

        state_file = (
            state_file_override
            if state_file_override is not None
            else Path(os.environ.get("STATE_FILE", "state/processed_messages.json"))
        )
        lookback_days = (
            lookback_days_override
            if lookback_days_override is not None
            else int(os.environ.get("LOOKBACK_DAYS", "7"))
        )
        match_by_role = os.environ.get("MATCH_BY_ROLE", "false").strip().lower() in {"1", "true", "yes"}

        return cls(
            mail_provider=mail_provider,
            kakao_email=mail_email,
            kakao_app_password=mail_password,
            kakao_imap_host=mail_host,
            kakao_imap_port=mail_port,
            kakao_imap_mailbox=mail_mailbox,
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
            notion_token=os.environ.get("NOTION_TOKEN", "").strip(),
            notion_database_id=os.environ.get("NOTION_DATABASE_ID", "").strip(),
            llm_provider=provider,
            llm_api_key=api_key,
            llm_model=llm_model,
            gemini_thinking_level=os.environ.get("GEMINI_THINKING_LEVEL", "low").strip().lower(),
            lookback_days=max(1, lookback_days),
            min_confidence=float(os.environ.get("MIN_CONFIDENCE", "0.65")),
            match_by_role=match_by_role,
            state_file=state_file,
            rules=rules,
            notion_schema=notion_schema,
        )

    def validate(self, *, dry_run: bool) -> None:
        missing: list[str] = []
        if not self.kakao_email:
            missing.append("MAIL_EMAIL (or KAKAO_EMAIL for kakao)")
        if not self.kakao_app_password:
            missing.append("MAIL_APP_PASSWORD (or KAKAO_APP_PASSWORD for kakao)")
        if not self.kakao_imap_host:
            missing.append("MAIL_IMAP_HOST (required for MAIL_PROVIDER=custom)")
        if not dry_run:
            if not self.slack_webhook_url:
                missing.append("SLACK_WEBHOOK_URL")
            if not self.notion_token:
                missing.append("NOTION_TOKEN")
            if not self.notion_database_id:
                missing.append("NOTION_DATABASE_ID")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
