"""Pure helper functions (no network). Heavily unit-tested."""
import contextlib
import html
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
ANY_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
GENERIC_PREFIXES = {
    "info", "hello", "hi", "contact", "support", "admin", "sales", "team", "office",
    "mail", "enquiries", "inquiries", "help", "service", "marketing", "hey", "noreply", "no-reply",
}

# Sheet columns / status flow
ACTIVE_STATUSES = ("sent", "followup1", "followup2")
FOLLOWUP_STATUS = {0: "sent", 1: "followup1", 2: "followup2"}
REPLY_STATUSES = ("replied", "not_interested", "unsubscribed")


# ---------- email ----------
def clean_email(v) -> str:
    return (v or "").strip().strip("<>").strip().lower()


def is_valid_email(e: str) -> bool:
    return bool(e) and bool(EMAIL_RE.match(e)) and ".." not in e


def is_generic_email(e: str) -> bool:
    return e.split("@")[0] in GENERIC_PREFIXES


def first_name(full: str) -> str:
    parts = re.split(r"\s+", (full or "").strip())
    for p in parts:
        p = p.strip(".,")
        if p and p.lower() not in ("dr", "mr", "mrs", "ms", "prof"):
            return p.capitalize() if p.islower() or p.isupper() else p
    return "there"


# ---------- time ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ms_to_iso(ms: int) -> str:
    return iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def iso_to_ms(s) -> int:
    dt = parse_iso(s)
    return int(dt.timestamp() * 1000) if dt else 0


def to_int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


# ---------- text ----------
def html_to_text(h: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", h or "")
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|li|tr)>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = "\n".join(line.strip() for line in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def snippet(text: str, n: int = 200) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


_WROTE_RE = re.compile(r"^\s*On .{5,200}wrote:?\s*$", re.I)


def strip_quoted(text: str) -> str:
    """Remove quoted original mail so our own 'reply stop' footer is never read as the lead's words."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out = []
    for i, line in enumerate(lines):
        s = line.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _WROTE_RE.match(line) or (s.lower().startswith("on ") and nxt.lower().startswith("wrote:")):
            break
        if s.lower().startswith(("-----original message", "________________________________")):
            break
        if re.match(r"^(from|sent):\s", s, re.I) and re.match(r"^(sent|to|date):\s", nxt, re.I):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


# ---------- templates ----------
def render(template: str, ctx: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(ctx.get(m.group(1), "")), template)


def load_sections(path: str) -> dict:
    """Parse templates/email_body.html -> {step: {'subject': str|None, 'html': str}}"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    out = {}
    for name, body in re.findall(r"<!--STEP:(\w+)-->(.*?)<!--/STEP-->", raw, re.S):
        m = re.search(r"<!--SUBJECT:(.*?)-->", body, re.S)
        subject = m.group(1).strip() if m else None
        body = re.sub(r"<!--SUBJECT:.*?-->", "", body, flags=re.S).strip()
        out[name] = {"subject": subject, "html": body}
    return out


# ---------- message classification ----------
UNSUB_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[\s-]?out|do not (contact|email)|don'?t (contact|email)|"
    r"stop (emailing|sending|contacting|messaging)|please stop)\b", re.I)
UNSUB_START_RE = re.compile(r"^\s*(stop|remove|unsubscribe)\b[\s.!]*$", re.I)
OOO_SUBJECT_RE = re.compile(
    r"(out of (the )?office|automatic reply|auto[\s-]?reply|autoreply|on (vacation|leave)|away from (the )?office)", re.I)


def looks_like_unsubscribe(text: str) -> bool:
    first = (text or "").strip().split("\n")[0] if (text or "").strip() else ""
    return bool(UNSUB_RE.search(text or "")) or bool(UNSUB_START_RE.match(first))


def is_bounce(msg: dict) -> bool:
    local = (msg.get("from_addr") or "").split("@")[0]
    ctype = (msg.get("headers", {}).get("content-type") or "").lower()
    return local in ("mailer-daemon", "postmaster") or "report-type=delivery-status" in ctype


def looks_like_ooo(msg: dict) -> bool:
    h = msg.get("headers", {})
    auto = (h.get("auto-submitted") or "").lower()
    if auto and auto != "no":
        return True
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return True
    if (h.get("precedence") or "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(OOO_SUBJECT_RE.search(msg.get("subject") or ""))


def is_from_me(msg: dict, me: str) -> bool:
    return (msg.get("from_addr") or "").lower() == me.lower()


def others_in_thread(messages, me: str):
    return [m for m in messages if not is_from_me(m, me)]


def failed_recipients(msg: dict, tracked: set, me: str = "") -> list:
    """Which tracked addresses does this bounce mail refer to?"""
    found = []
    hdr = msg.get("headers", {}).get("x-failed-recipients", "")
    for e in ANY_EMAIL_RE.findall(hdr):
        found.append(e.lower())
    for e in ANY_EMAIL_RE.findall(msg.get("body", "") or ""):
        found.append(e.lower())
    seen, out = set(), []
    for e in found:
        if e in tracked and e != me.lower() and e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ---------- stats ----------
def compute_stats(rows) -> dict:
    counts = {}
    sent = 0
    for r in rows:
        st = (r.get("status") or "new").lower()
        counts[st] = counts.get(st, 0) + 1
        if (r.get("sent_at") or "").strip():
            sent += 1
    replied_any = sum(counts.get(s, 0) for s in REPLY_STATUSES)
    bounced = counts.get("bounced", 0)
    return {
        "total": len(rows),
        "sent": sent,
        "counts": counts,
        "replied": counts.get("replied", 0),
        "replied_any": replied_any,
        "bounced": bounced,
        "reply_rate": (replied_any / sent) if sent else 0.0,
        "positive_rate": (counts.get("replied", 0) / sent) if sent else 0.0,
        "bounce_rate": (bounced / sent) if sent else 0.0,
    }


# ---------- lock ----------
class LockBusy(Exception):
    pass


@contextlib.contextmanager
def file_lock(path: str, stale_seconds: int = 3600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) > stale_seconds:
        os.remove(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise LockBusy(path)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


# ---------- email filtering ----------
AUTOMATED_SENDERS_RE = re.compile(
    r"\b(noreply|no-reply|donotreply|do-not-reply|automated|mailer-daemon|postmaster|"
    r"notifications|notification|newsletter|news@|marketing|updates|promo|"
    r"billing|receipts|security|security-noreply|alerts|digest|feedback|invitations)\b",
    re.I,
)


def is_promotional_or_automated(msg: dict) -> bool:
    """Return True if message is promotional, automated, newsletter, or system alert."""
    if not isinstance(msg, dict):
        return False

    # 1. Gmail category labels
    labels = set(msg.get("labels") or [])
    if labels & {"CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}:
        return True

    # 2. Marketing / list headers
    headers = msg.get("headers") or {}
    if "list-unsubscribe" in headers:
        return True
    if headers.get("precedence") in ("bulk", "list", "junk"):
        return True
    auto_sub = str(headers.get("auto-submitted", "")).lower()
    if auto_sub and auto_sub != "no":
        return True

    # 3. Known automated sender keywords
    addr = str(msg.get("from_addr", "")).lower()
    if AUTOMATED_SENDERS_RE.search(addr):
        return True

    return False
