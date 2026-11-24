"""Non-destructive IMAP client using verified TLS and BODY.PEEK[]."""

from __future__ import annotations

import email
import hashlib
import html
import imaplib
import logging
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable, Iterable, Optional

from recruit_mail_observer.models import EmailMessage

LOGGER = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
HTML_TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
NEWLINES_RE = re.compile(r"\n{3,}")
DEFAULT_IMAP_TIMEOUT_SECONDS = 30


def create_verified_imap_connection(
    host: str,
    port: int,
    *,
    timeout: int = DEFAULT_IMAP_TIMEOUT_SECONDS,
) -> imaplib.IMAP4_SSL:
    """Create an IMAP4_SSL connection with strict certificate and hostname verification."""
    ssl_context = ssl.create_default_context()
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=timeout)


def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            encoding = charset or "utf-8"
            try:
                parts.append(chunk.decode(encoding, errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def decode_payload(payload: bytes, charset: Optional[str]) -> str:
    candidates = [charset, "utf-8", "cp949", "euc-kr", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def html_to_text(raw_html: str) -> str:
    cleaned = SCRIPT_STYLE_RE.sub(" ", raw_html)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", cleaned)
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    return normalize_body_text(cleaned)


def normalize_body_text(text: str) -> str:
    lines = [WHITESPACE_RE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").splitlines()]
    joined = "\n".join(lines)
    return NEWLINES_RE.sub("\n\n", joined).strip()


def extract_body_text(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = decode_payload(payload, part.get_content_charset())
            if content_type == "text/plain":
                plain_parts.append(normalize_body_text(decoded))
            elif content_type == "text/html":
                html_parts.append(html_to_text(decoded))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            decoded = decode_payload(payload, msg.get_content_charset())
            if msg.get_content_type() == "text/html":
                html_parts.append(html_to_text(decoded))
            else:
                plain_parts.append(normalize_body_text(decoded))

    if plain_parts:
        return "\n\n".join(p for p in plain_parts if p).strip()
    return "\n\n".join(p for p in html_parts if p).strip()


def parse_raw_email(uid: str, raw_bytes: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw_bytes)
    raw_from = decode_mime_header(msg.get("From"))
    sender_name, sender_email = parseaddr(raw_from)
    sender_name = decode_mime_header(sender_name)
    subject = decode_mime_header(msg.get("Subject"))

    date_header = msg.get("Date")
    received_dt = datetime.now(KST)
    if date_header:
        try:
            parsed_dt = parsedate_to_datetime(date_header)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            received_dt = parsed_dt.astimezone(KST)
        except (TypeError, ValueError):
            pass

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # Hash raw message bytes so fallback ID is deterministic even when Date is also absent.
        stable_digest = hashlib.sha256(raw_bytes).hexdigest()[:24]
        message_id = f"<fallback-{stable_digest}>"

    message_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    body_text = extract_body_text(msg)

    return EmailMessage(
        uid=uid,
        message_id=message_id,
        message_hash=message_hash,
        sender_name=sender_name,
        sender_email=sender_email.lower(),
        subject=subject,
        received_at=received_dt.isoformat(timespec="seconds"),
        received_date=received_dt.strftime("%Y-%m-%d"),
        body_text=body_text,
    )


class KakaoImapClient:
    """Reads unread emails via IMAP without marking them as read."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        email_address: str,
        app_password: str,
        mailbox: str = "INBOX",
        imap_factory: Callable[[str, int], imaplib.IMAP4_SSL] = create_verified_imap_connection,
    ) -> None:
        self.host = host
        self.port = port
        self.email_address = email_address
        self.app_password = app_password
        self.mailbox = mailbox
        self._imap_factory = imap_factory
        self.last_fetch_errors: list[str] = []

    def fetch_unread_since(
        self,
        *,
        lookback_days: int = 7,
        max_emails: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[EmailMessage]:
        reference_now = now or datetime.now(KST)
        since_date = (reference_now - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        self.last_fetch_errors = []

        client = self._imap_factory(self.host, self.port)
        try:
            client.login(self.email_address, self.app_password)
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError(f"Failed to select mailbox {self.mailbox}: {status}")

            status, data = client.uid("SEARCH", None, "UNSEEN", "SINCE", since_date)
            if status != "OK":
                raise RuntimeError(f"IMAP UID SEARCH failed: {status}")

            raw_uids = data[0].decode("ascii", errors="ignore").split() if data and data[0] else []
            if max_emails is not None and max_emails > 0:
                raw_uids = raw_uids[-max_emails:]

            messages: list[EmailMessage] = []
            for uid in self._iter_uids(raw_uids):
                try:
                    # BODY.PEEK[] ensures the \Seen flag is NOT set on the server.
                    fetch_status, fetch_data = client.uid("FETCH", uid, "(BODY.PEEK[])")
                    if fetch_status != "OK" or not fetch_data:
                        self.last_fetch_errors.append(f"uid={uid}:fetch_failed")
                        LOGGER.warning("IMAP FETCH failed for uid=%s (status=%s)", uid, fetch_status)
                        continue
                    raw_bytes = self._extract_raw_bytes(fetch_data)
                    if raw_bytes is None:
                        self.last_fetch_errors.append(f"uid={uid}:empty_payload")
                        LOGGER.warning("IMAP FETCH returned empty payload for uid=%s", uid)
                        continue
                    messages.append(parse_raw_email(uid, raw_bytes))
                except Exception as exc:
                    self.last_fetch_errors.append(f"uid={uid}:parse_error:{type(exc).__name__}")
                    LOGGER.warning("Failed to parse email uid=%s (%s)", uid, type(exc).__name__)
            return messages
        finally:
            try:
                client.logout()
            except Exception:
                pass

    @staticmethod
    def _iter_uids(uids: Iterable[str]) -> Iterable[str]:
        for uid in uids:
            cleaned = uid.strip()
            if cleaned:
                yield cleaned

    @staticmethod
    def _extract_raw_bytes(fetch_data: list[object]) -> Optional[bytes]:
        for item in fetch_data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        return None
