"""Detect replies/bounces/OOO, classify intent, update sheet, alert on Telegram."""
import json
import os
import time

from utils.helpers import (ACTIVE_STATUSES, clean_email, failed_recipients, is_bounce, is_promotional_or_automated,
                           iso_to_ms, looks_like_ooo, ms_to_iso, now_utc, iso, others_in_thread, snippet, strip_quoted)
from utils.logger import get_logger

log = get_logger("agent.replies")

INTENT_TO_STATUS = {
    "interested": "replied", "question": "replied", "referral": "replied", "other": "replied",
    "not_interested": "not_interested", "unsubscribe": "unsubscribed",
}


def _load_seen_ids(path: str) -> set:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _save_seen_ids(path: str, seen: set):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        kept = list(seen)[-2000:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kept, f)
    except Exception as e:
        log.warning("Could not save seen email IDs: %s", e)


def _bounce_update(sheets, row, msg):
    sheets.update(row["_row"], {"status": "bounced", "last_reply_at": ms_to_iso(msg["date_ms"]),
                                "notes": "bounce: " + snippet(msg.get("body", ""), 140)})
    row["status"] = "bounced"


def run(cfg, gmail, sheets, telegram, analyzer) -> dict:
    res = {"replied": 0, "not_interested": 0, "unsubscribed": 0, "bounced": 0, "ooo": 0, "checked": 0, "errors": 0, "new_inbox": 0}
    rows = sheets.get_rows() if sheets else []
    active = [r for r in rows if (r.get("status") or "").lower() in ACTIVE_STATUSES and r.get("thread_id")]
    by_email = {clean_email(r["email"]): r for r in active}

    # ---- 1) bounce sweep (catches bounces that landed outside the thread) ----
    if hasattr(gmail, "search_bounces"):
        try:
            for b in gmail.search_bounces(cfg.bounce_lookback_days):
                for em in failed_recipients(b, set(by_email), gmail.me):
                    row = by_email[em]
                    if (row.get("status") or "").lower() == "bounced":
                        continue
                    _bounce_update(sheets, row, b)
                    res["bounced"] += 1
                    telegram.send(f"📭 Bounce: {em} ({row.get('company', '')})")
        except Exception as e:
            res["errors"] += 1
            log.warning("Bounce sweep failed: %s", e)

    # ---- 2) per-thread reply check for leads in sheet ----
    for row in active:
        if (row.get("status") or "").lower() not in ACTIVE_STATUSES:
            continue  # changed by bounce sweep above
        res["checked"] += 1
        try:
            msgs = gmail.get_thread(row["thread_id"])
        except Exception as e:
            res["errors"] += 1
            log.warning("Row %s: thread fetch failed: %s", row["_row"], e)
            continue
        last_s = iso_to_ms(row.get("last_reply_at")) // 1000
        inbound = [m for m in others_in_thread(msgs, gmail.me) if m["date_ms"] // 1000 > last_s]
        outcome, ooo_msg = None, None
        for m in inbound:
            if is_bounce(m):
                outcome = ("bounce", m, None)
                break
            if looks_like_ooo(m):
                ooo_msg = m
                continue
            body = strip_quoted(m["body"])
            analysis = analyzer.analyze(body, row.get("name", ""), row.get("company", ""))
            outcome = ("reply", m, analysis)
            break

        if outcome is None:
            if ooo_msg:
                sheets.update(row["_row"], {"last_reply_at": ms_to_iso(ooo_msg["date_ms"]),
                                            "notes": "auto-reply/OOO seen " + ms_to_iso(ooo_msg["date_ms"])})
                res["ooo"] += 1
            continue

        kind, m, analysis = outcome
        if kind == "bounce":
            _bounce_update(sheets, row, m)
            res["bounced"] += 1
            telegram.send(f"📭 Bounce: {row['email']} ({row.get('company', '')})")
            continue

        status = INTENT_TO_STATUS.get(analysis["intent"], "replied")
        sheets.update(row["_row"], {
            "status": status,
            "last_reply_at": ms_to_iso(m["date_ms"]),
            "reply_intent": analysis["intent"],
            "reply_snippet": snippet(strip_quoted(m["body"]), 300),
            "notes": analysis.get("summary", ""),
        })
        res[status] += 1
        who = f"{row.get('name') or row['email']} ({row.get('company', '')})"
        
        # Analyze in Bengali for rich Telegram notification
        bn = analyzer.analyze_email_bn(m.get("from_raw", who), row.get("subject", ""), strip_quoted(m["body"]))
        msg = (
            f"🔥 Reply from {who} (ক্লায়েন্টের নতুন রিপ্লাই)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 ক্লায়েন্ট: {who}\n"
            f"📧 ইমেইল: {row['email']}\n"
            f"🏷️ স্ট্যাটাস: {status}\n\n"
            f"📝 সারসংক্ষেপ (বাংলায়):\n{bn.get('summary_bn', analysis.get('summary', ''))}\n\n"
            f"💡 করণীয়:\n{bn.get('action_bn', 'ইনবক্স চেক করুন')}"
        )
        if bn.get("suggested_reply"):
            msg += f"\n\n✍️ সম্ভাব্য রিপ্লাই:\n{bn.get('suggested_reply')}"
        msg += "\n━━━━━━━━━━━━━━━━━━"
        telegram.send(msg)
        log.info("Row %s -> %s (%s via %s)", row["_row"], status, analysis["intent"], analysis.get("source"))

    # ---- 3) Inbox Unread Emails Check (General incoming emails & Bengali AI analysis) ----
    seen_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seen_inbox_emails.json")
    seen_ids = _load_seen_ids(seen_file)

    if hasattr(gmail, "get_unread_inbox_messages"):
        try:
            unread_msgs = gmail.get_unread_inbox_messages(max_results=20)
            for m in unread_msgs:
                mid = m.get("id")
                if not mid or mid in seen_ids:
                    continue
                if is_bounce(m) or is_promotional_or_automated(m):
                    seen_ids.add(mid)
                    log.info("Skipped promotional/automated email from: %s", m.get("from_raw"))
                    continue

                # Run Gemini analysis in Bengali
                bn = analyzer.analyze_email_bn(m.get("from_raw", ""), m.get("subject", ""), m.get("body", ""))

                # Filter out promotional or automated emails identified by Gemini AI
                if not bn.get("is_work_or_human", True):
                    seen_ids.add(mid)
                    log.info("Gemini classified as promotional/automated, skipped: %s", m.get("from_raw"))
                    continue

                alert_text = (
                    f"📩 নতুন ইমেইল এসেছে!\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 প্রেরক: {m.get('from_raw', 'অজানা')}\n"
                    f"📌 বিষয়: {m.get('subject') or '(কোন বিষয় নেই)'}\n"
                    f"🏷️ ধরন: {bn.get('intent_bn', 'সাধারণ বার্তা')}\n\n"
                    f"📝 সারসংক্ষেপ (বাংলায়):\n{bn.get('summary_bn', '')}\n\n"
                    f"💡 করণীয়:\n{bn.get('action_bn', '')}"
                )
                if bn.get("suggested_reply"):
                    alert_text += f"\n\n✍️ সম্ভাব্য রিপ্লাই:\n{bn.get('suggested_reply')}"
                sent_ok = telegram.send(alert_text)
                if sent_ok or not telegram.enabled:
                    seen_ids.add(mid)
                    res["new_inbox"] += 1
                    log.info("Telegram notified for new inbox email from: %s", m.get("from_raw"))
                else:
                    log.warning("Telegram alert failed for: %s (will retry on next run)", m.get("from_raw"))
                time.sleep(1.2)

            _save_seen_ids(seen_file, seen_ids)
        except Exception as e:
            res["errors"] += 1
            log.warning("Inbox unread check failed: %s", e)

    return res
