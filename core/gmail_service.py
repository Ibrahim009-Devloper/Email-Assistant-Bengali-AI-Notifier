"""Gmail API: auth, send (threaded), read threads, bounce search."""
import base64
import os
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.helpers import html_to_text
from utils.logger import get_logger

log = get_logger("agent.gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


class SendError(Exception):
    """Transient send failure."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class PermanentSendError(SendError):
    """Bad recipient / malformed request. Do not retry."""


class QuotaError(SendError):
    """Rate/quota limit hit. Stop the run."""


# ---------- auth ----------
def get_credentials(cfg) -> Credentials:
    creds = None
    if os.path.exists(cfg.google_token_file):
        creds = Credentials.from_authorized_user_file(cfg.google_token_file, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, cfg.google_token_file)
            return creds
        except RefreshError as e:
            log.error("Refresh token rejected (%s). Re-run: python main.py auth", e)
            raise
    if not os.path.exists(cfg.google_credentials_file):
        raise FileNotFoundError(f"{cfg.google_credentials_file} not found")
    flow = InstalledAppFlow.from_client_secrets_file(cfg.google_credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds, cfg.google_token_file)
    return creds


def _save(creds, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------- parsing (pure, testable) ----------
def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk(part, out):
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        out.setdefault(mime, []).append(_decode(data))
    for p in part.get("parts") or []:
        _walk(p, out)


def extract_body(payload: dict) -> str:
    out = {}
    _walk(payload, out)
    if out.get("text/plain"):
        return "\n".join(out["text/plain"]).strip()
    if out.get("text/html"):
        return html_to_text("\n".join(out["text/html"]))
    return ""


def parse_message(raw: dict) -> dict:
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    from_raw = headers.get("from", "")
    return {
        "id": raw["id"],
        "thread_id": raw.get("threadId", ""),
        "from_raw": from_raw,
        "from_addr": parseaddr(from_raw)[1].lower(),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "headers": headers,
        "body": extract_body(payload),
        "date_ms": int(raw.get("internalDate", 0)),
        "labels": raw.get("labelIds", []),
    }


def build_mime(me, sender_name, to, subject, text, html, in_reply_to=None, references=None) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = formataddr((sender_name, me))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=me.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{me}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


# ---------- service ----------
class GmailService:
    def __init__(self, creds, cfg):
        self.cfg = cfg
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.me = self.svc.users().getProfile(userId="me").execute()["emailAddress"].lower()

    def send(self, to, subject, text, html, thread_id=None, in_reply_to=None, references=None) -> dict:
        msg = build_mime(self.me, self.cfg.sender_name, to, subject, text, html, in_reply_to, references)
        body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        try:
            res = self.svc.users().messages().send(userId="me", body=body).execute(num_retries=3)
            meta = self.svc.users().messages().get(
                userId="me", id=res["id"], format="metadata", metadataHeaders=["Message-ID"]
            ).execute(num_retries=3)
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 403):
                raise QuotaError(f"quota/limit: {e}", status)
            if status in (400, 404):
                raise PermanentSendError(f"rejected: {e}", status)
            raise SendError(str(e), status)
        except Exception as e:  # network etc.
            raise SendError(str(e))
        hdrs = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
        return {
            "id": res["id"],
            "thread_id": res.get("threadId", thread_id or ""),
            "message_id": hdrs.get("message-id", msg["Message-ID"]),
        }

    def get_thread(self, thread_id: str) -> list:
        t = self.svc.users().threads().get(userId="me", id=thread_id, format="full").execute(num_retries=3)
        msgs = [parse_message(m) for m in t.get("messages", [])]
        return sorted(msgs, key=lambda m: m["date_ms"])

    def search_bounces(self, days: int = 7) -> list:
        q = f"(from:mailer-daemon OR from:postmaster) newer_than:{int(days)}d"
        out, token = [], None
        while True:
            res = self.svc.users().messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute(num_retries=3)
            for m in res.get("messages", []):
                raw = self.svc.users().messages().get(userId="me", id=m["id"], format="full").execute(num_retries=3)
                out.append(parse_message(raw))
            token = res.get("nextPageToken")
            if not token:
                return out

    def get_unread_inbox_messages(self, max_results: int = 15) -> list:
        """Fetch unread messages from Inbox (excluding sent by me)."""
        q = "is:unread in:inbox -from:me"
        out = []
        try:
            res = self.svc.users().messages().list(userId="me", q=q, maxResults=max_results).execute(num_retries=3)
            for m in res.get("messages", []):
                raw = self.svc.users().messages().get(userId="me", id=m["id"], format="full").execute(num_retries=3)
                out.append(parse_message(raw))
        except Exception as e:
            log.warning("Failed to fetch unread inbox messages: %s", e)
        return sorted(out, key=lambda m: m["date_ms"])
