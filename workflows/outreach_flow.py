"""Send first-touch + follow-ups, log everything to the sheet."""
import random
import time
from datetime import timedelta

from core.gmail_service import PermanentSendError, QuotaError, SendError
from utils.helpers import (FOLLOWUP_STATUS, compute_stats, clean_email, first_name, html_to_text, iso,
                           is_generic_email, is_valid_email, load_sections, now_utc, others_in_thread,
                           parse_iso, render, to_int)
from utils.logger import get_logger

log = get_logger("agent.outreach")


def _context(cfg, row):
    hook = (row.get("hook") or "").strip()
    if hook and hook[-1] not in ".!?":
        hook += "."
    return {
        "first_name": first_name(row.get("name", "")),
        "company": row.get("company", "") or "your team",
        "hook_line": (hook + " ") if hook else "",
        "offer_line": cfg.offer_line,
        "sender_name": cfg.sender_name,
        "sender_address": cfg.sender_address,
    }


def daily_cap(cfg, rows, now) -> int:
    """Warm-up: start small, grow each day since first send, never above DAILY_LIMIT."""
    firsts = [parse_iso(r.get("sent_at")) for r in rows if parse_iso(r.get("sent_at"))]
    if not firsts:
        return min(cfg.daily_limit, cfg.warmup_start)
    days = (now.date() - min(firsts).date()).days
    return min(cfg.daily_limit, cfg.warmup_start + days * cfg.warmup_step)


def sent_today(rows, now) -> int:
    today = now.strftime("%Y-%m-%d")
    return sum(1 for r in rows if (r.get("last_sent_at") or "").startswith(today))


def run(cfg, gmail, sheets, telegram, sleep=time.sleep, rng=random, now=None) -> dict:
    now = now or now_utc()
    res = {"sent": 0, "followups": 0, "skipped": 0, "errors": 0, "aborted": None, "closed": 0}
    rows = sheets.get_rows()

    # --- safety brake: bounce rate ---
    st = compute_stats(rows)
    if st["sent"] >= cfg.bounce_min_sample and st["bounce_rate"] > cfg.bounce_pause_rate:
        res["aborted"] = "bounce_rate"
        telegram.send(f"⛔ Outreach paused: bounce rate {st['bounce_rate']:.1%} > {cfg.bounce_pause_rate:.0%}. "
                      f"Clean the list, then resume.")
        log.error("Paused: bounce rate %.1f%%", st["bounce_rate"] * 100)
        return res

    sections = load_sections(cfg.template_path)
    last_followup_day = cfg.followup_days[-1] if cfg.followup_days else 0

    # --- close finished sequences (no sending involved, no cap) ---
    for r in rows:
        if (r.get("status") or "").lower() == f"followup{len(cfg.followup_days)}":
            sent_dt = parse_iso(r.get("sent_at"))
            if sent_dt and now - sent_dt >= timedelta(days=last_followup_day + cfg.no_reply_after_days):
                if not cfg.dry_run:
                    sheets.update(r["_row"], {"status": "no_reply"})
                r["status"] = "no_reply"
                res["closed"] += 1

    # --- build queue: follow-ups first, then new ---
    followups, fresh = [], []
    for r in rows:
        status = (r.get("status") or "new").lower()
        if status == "new":
            fresh.append(r)
        elif status in ("sent", "followup1"):
            n = 0 if status == "sent" else 1
            if n >= len(cfg.followup_days):
                continue
            sent_dt = parse_iso(r.get("sent_at"))
            if sent_dt and r.get("thread_id") and now - sent_dt >= timedelta(days=cfg.followup_days[n]):
                followups.append((n, r))

    budget = min(cfg.per_run_limit, max(0, daily_cap(cfg, rows, now) - sent_today(rows, now)))
    queue = [("followup", n, r) for n, r in followups] + [("new", 0, r) for r in fresh]
    log.info("Queue: %d followups, %d new | budget %d | dry_run=%s", len(followups), len(fresh), budget, cfg.dry_run)

    seen_emails = {clean_email(r.get("email")) for r in rows if (r.get("status") or "new").lower() != "new"}
    consecutive_fail = 0
    done = 0
    for idx, (kind, n, r) in enumerate(queue):
        if done >= budget:
            break
        email = clean_email(r.get("email"))
        row_num = r["_row"]

        if kind == "new":
            if not is_valid_email(email):
                res["skipped"] += 1
                if not cfg.dry_run:
                    sheets.update(row_num, {"status": "invalid", "notes": "invalid email format"})
                continue
            if is_generic_email(email):
                res["skipped"] += 1
                if not cfg.dry_run:
                    sheets.update(row_num, {"status": "skipped", "notes": "generic address, owner email required"})
                continue
            if email in seen_emails:
                res["skipped"] += 1
                if not cfg.dry_run:
                    sheets.update(row_num, {"status": "skipped", "notes": "duplicate email"})
                continue

        ctx = _context(cfg, r)
        refs = in_reply_to = thread_id = None
        if kind == "followup":
            # Last-second guard: never follow up if anyone (or a bounce) already wrote in the thread.
            try:
                msgs = gmail.get_thread(r["thread_id"])
            except Exception as e:
                log.warning("Row %s: thread fetch failed, skipping followup (%s)", row_num, e)
                res["skipped"] += 1
                continue
            if others_in_thread(msgs, gmail.me):
                log.info("Row %s: thread has inbound mail, followup skipped (reply monitor will handle)", row_num)
                res["skipped"] += 1
                continue
            ids = [m["message_id"] for m in msgs if m.get("message_id")]
            in_reply_to = ids[-1] if ids else r.get("rfc_message_id") or None
            refs = " ".join(ids) if ids else in_reply_to
            thread_id = r["thread_id"]
            sec = sections[FOLLOWUP_STATUS[n + 1]]
            subj = r.get("subject") or "follow up"
            subject = subj if subj.lower().startswith("re:") else "Re: " + subj
        else:
            sec = sections["initial"]
            subject = render(sec["subject"] or "quick question", ctx)

        html = render(sec["html"], ctx)
        text = html_to_text(html)

        if cfg.dry_run:
            log.info("[DRY RUN] would send %s to %s | subject=%r", "followup%d" % (n + 1) if kind == "followup" else "initial", email, subject)
            done += 1
            continue

        try:
            out = gmail.send(email, subject, text, html, thread_id=thread_id, in_reply_to=in_reply_to, references=refs)
        except QuotaError as e:
            res["aborted"] = "quota"
            telegram.send(f"⚠️ Gmail quota/limit hit, run stopped: {e}")
            log.error("Quota hit: %s", e)
            break
        except PermanentSendError as e:
            res["errors"] += 1
            sheets.update(row_num, {"status": "invalid", "notes": f"send rejected: {str(e)[:150]}"})
            continue
        except SendError as e:
            res["errors"] += 1
            consecutive_fail += 1
            log.warning("Send failed for %s: %s", email, e)
            if consecutive_fail >= 3:
                res["aborted"] = "send_failures"
                telegram.send(f"⚠️ 3 send failures in a row, run stopped. Last: {str(e)[:150]}")
                break
            continue
        consecutive_fail = 0

        now_s = iso(now_utc() if now is None else now)
        if kind == "new":
            fields = {"status": "sent", "thread_id": out["thread_id"], "rfc_message_id": out["message_id"],
                      "subject": subject, "sent_at": now_s, "last_sent_at": now_s, "followups": 0, "notes": ""}
            res["sent"] += 1
        else:
            fields = {"status": FOLLOWUP_STATUS[n + 1] if n + 1 <= 2 else "followup2",
                      "last_sent_at": now_s, "followups": n + 1}
            res["followups"] += 1
        try:
            sheets.update(row_num, fields)
        except Exception as e:
            # Mail is out but not logged: stop immediately so we never double-send.
            res["aborted"] = "sheet_write"
            telegram.send(f"🚨 Mail sent to {email} but sheet update FAILED ({e}). Fix manually (row {row_num}); run stopped.")
            log.error("Sheet write failed after send for %s: %s", email, e)
            break
        seen_emails.add(email)
        done += 1
        log.info("Sent %s to %s (row %d)", "followup%d" % (n + 1) if kind == "followup" else "initial", email, row_num)

        if done < budget and idx < len(queue) - 1:
            sleep(rng.uniform(cfg.delay_min, cfg.delay_max))

    if (res["sent"] or res["followups"]) and not cfg.dry_run:
        telegram.send(f"📤 Outreach run: {res['sent']} new, {res['followups']} follow-ups, "
                      f"{res['skipped']} skipped, {res['errors']} errors.")
    return res
