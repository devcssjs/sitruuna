"""Reads college mail through the Gmail API (read-only)."""
from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr
from html.parser import HTMLParser

from googleapiclient.discovery import build

from .config import Settings
from .google_auth import GMAIL_SCOPES, GMAIL_TOKEN, get_creds

SKIP_SMALL_IMAGES_BYTES = 20_000      # logos / signature images
MAX_ATTACHMENT_BYTES = 15_000_000


@dataclass
class Attachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class Email:
    msg_id: str
    sender: str
    subject: str
    sent_at: datetime
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    link: str = ""


class GmailReader:
    def __init__(self, settings: Settings):
        self.s = settings
        creds = get_creds(GMAIL_TOKEN, GMAIL_SCOPES)
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.account = self.svc.users().getProfile(userId="me").execute().get("emailAddress", "")

    # ---------- searching ----------
    def build_query(self, since: datetime | None, days: int | None = None) -> str:
        parts = []
        if days:
            parts.append(f"newer_than:{int(days)}d")
        elif since is None:
            parts.append(f"newer_than:{self.s.get('gmail', 'backfill_days', default=60)}d")
        else:
            # 1 hour overlap; already-read emails are skipped by ID anyway
            parts.append(f"after:{int((since - timedelta(hours=1)).timestamp())}")
        allowed = self.s.get("gmail", "allowed_senders", default=[]) or []
        if allowed:
            parts.append("{" + " ".join(f"from:{a}" for a in allowed) + "}")
        extra = self.s.get("gmail", "extra_query", default="")
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def list_ids(self, query: str, limit: int = 500) -> list[str]:
        ids, token = [], None
        while len(ids) < limit:
            resp = self.svc.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=token
            ).execute()
            ids += [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        ids.reverse()  # oldest first, so later corrections override earlier mails
        return ids[:limit]

    # ---------- reading one email ----------
    def fetch(self, msg_id: str) -> Email:
        msg = self.svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=self.s.tz)

        plain, htmls, atts = [], [], []
        self._walk(msg_id, msg["payload"], plain, htmls, atts)
        body = "\n".join(plain).strip() or html_to_text("\n".join(htmls))

        return Email(
            msg_id=msg_id,
            sender=headers.get("from", ""),
            subject=headers.get("subject", "(no subject)"),
            sent_at=sent_at,
            body=body,
            attachments=atts,
            link=f"https://mail.google.com/mail/u/?authuser={self.account}#all/{msg_id}",
        )

    def _walk(self, msg_id, part, plain, htmls, atts):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename") or ""

        if part.get("parts"):
            for p in part["parts"]:
                self._walk(msg_id, p, plain, htmls, atts)
            return

        if filename or body.get("attachmentId"):
            size = body.get("size", 0)
            if size > MAX_ATTACHMENT_BYTES:
                return
            if mime.startswith("image/") and size < SKIP_SMALL_IMAGES_BYTES:
                return
            data = self._attachment_bytes(msg_id, body)
            if data:
                atts.append(Attachment(filename or "attachment", mime, data))
            return

        if "data" in body:
            text = _b64(body["data"]).decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                htmls.append(text)

    def _attachment_bytes(self, msg_id, body) -> bytes:
        if "data" in body:
            return _b64(body["data"])
        att = self.svc.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=body["attachmentId"]
        ).execute()
        return _b64(att["data"])


def sender_allowed(sender: str, settings: Settings) -> bool:
    """Second safety check on top of the Gmail search filter."""
    addr = parseaddr(sender)[1].lower()
    for s in settings.get("gmail", "skip_senders", default=[]) or []:
        if s.lower() in addr:
            return False
    allowed = settings.get("gmail", "allowed_senders", default=[]) or []
    return not allowed or any(a.lower() in addr for a in allowed)


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self._skip = [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    p = _TextExtractor()
    p.feed(raw)
    text = html.unescape("".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()
