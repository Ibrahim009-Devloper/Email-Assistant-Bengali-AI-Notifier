"""Entry point.

  python main.py auth        one-time Google login (run on a PC with a browser)
  python main.py doctor      check config + connections
  python main.py outreach    send due mails (respects window/limits)  [--force to ignore send window]
  python main.py replies     check replies/bounces
  python main.py all         replies + outreach + dashboard (one tick)
  python main.py loop        run forever (tick every POLL_MINUTES)
  python main.py dashboard   refresh Dashboard tab
"""
import argparse
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import load_config, validate
from utils.helpers import LockBusy, compute_stats, file_lock
from utils.logger import get_logger

cfg = load_config()
log = get_logger("agent.main", cfg.log_dir)


def in_send_window(cfg, now=None) -> bool:
    now = now or datetime.now(ZoneInfo(cfg.send_tz))
    if cfg.weekdays_only and now.weekday() >= 5:
        return False
    return cfg.send_start_hour <= now.hour < cfg.send_end_hour


def build_services(cfg):
    from core.ai_analyzer import AIAnalyzer
    from core.gmail_service import GmailService, get_credentials
    from core.sheets_service import SheetsService
    from core.telegram_service import TelegramService

    creds = get_credentials(cfg)
    return {
        "gmail": GmailService(creds, cfg),
        "sheets": SheetsService.connect(creds, cfg),
        "telegram": TelegramService(cfg),
        "analyzer": AIAnalyzer(cfg),
    }


def tick(cfg, s, force_window=False, do_outreach=True, do_replies=True):
    from workflows import outreach_flow, reply_monitor_flow

    out = {}
    if do_replies:
        out["replies"] = reply_monitor_flow.run(cfg, s["gmail"], s["sheets"], s["telegram"], s["analyzer"])
        log.info("replies: %s", out["replies"])
    if do_outreach:
        if force_window or in_send_window(cfg):
            out["outreach"] = outreach_flow.run(cfg, s["gmail"], s["sheets"], s["telegram"])
            log.info("outreach: %s", out["outreach"])
        else:
            log.info("Outside send window (%s %02d-%02d), outreach skipped", cfg.send_tz, cfg.send_start_hour, cfg.send_end_hour)
    s["sheets"].write_dashboard(compute_stats(s["sheets"].get_rows()))
    return out


def cmd_doctor():
    problems = validate(cfg, for_send=True)
    for p in problems:
        print("✗", p)
    try:
        s = build_services(cfg)
        print("✓ Gmail connected as", s["gmail"].me)
        rows = s["sheets"].get_rows()
        print(f"✓ Sheet OK: {len(rows)} leads, stats: {compute_stats(rows)['counts']}")
        has_ai = bool(cfg.gemini_api_key or cfg.anthropic_api_key)
        print("✓ AI analyzer:", f"LLM ({cfg.gemini_model or 'Gemini'})" if has_ai else "rules only (no GEMINI_API_KEY)")
    except Exception as e:
        print("✗ Connection error:", e)
        return 1
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Email automation agent")
    ap.add_argument(
        "cmd",
        nargs="?",
        default=None,
        choices=["auth", "doctor", "outreach", "replies", "all", "loop", "dashboard"],
        help="Action to run (default: auto)",
    )
    ap.add_argument("--force", action="store_true", help="ignore send window")
    a = ap.parse_args(argv)

    cmd = a.cmd
    if not cmd:
        if not os.path.exists(cfg.google_token_file):
            print("\n[Auto] token.json not found -> Starting Google Auth login in browser...")
            cmd = "auth"
        else:
            print("\n[Auto] AI Email Assistant active: checking new emails & analyzing in Bengali...")
            cmd = "all"

    if cmd == "auth":
        if not os.path.exists(cfg.google_credentials_file):
            print(f"✗ Error: {cfg.google_credentials_file} not found! Please place your credentials.json in the project folder first.")
            return 1
        from core.gmail_service import get_credentials
        get_credentials(cfg)
        print("✓ token saved:", cfg.google_token_file)
        return 0
    if cmd == "doctor":
        return cmd_doctor()

    # for_send is only required if someone explicitly asks for outreach
    problems = validate(cfg, for_send=(cmd == "outreach"))
    if problems:
        print("\n⚠️ Configuration issue in .env:")
        for p in problems:
            log.error("Config: %s", p)
            print("  ✗", p)
        print("\nPlease fix these in .env and run again.\n")
        return 1
    s = build_services(cfg)

    if cmd == "dashboard":
        s["sheets"].write_dashboard(compute_stats(s["sheets"].get_rows()))
        return 0

    if cmd in ("outreach", "replies", "all"):
        try:
            with file_lock(cfg.lock_path):
                # Automatic cold outreach is OFF unless explicitly requested via "python main.py outreach"
                tick(cfg, s, force_window=a.force, do_outreach=(cmd == "outreach"), do_replies=True)
        except LockBusy:
            log.warning("Another run is active, exiting")
            print("⚠️ Another run is active, exiting.")
        return 0

    # loop (runs forever monitoring new emails every POLL_MINUTES)
    last_alert = 0.0
    log.info("Loop started: monitoring new emails every %d min", cfg.poll_minutes)
    while True:
        try:
            with file_lock(cfg.lock_path):
                tick(cfg, s, force_window=a.force, do_outreach=False, do_replies=True)
        except LockBusy:
            log.warning("Tick skipped, lock busy")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("Tick failed: %s", e)
            if time.time() - last_alert > 3600:
                s["telegram"].send(f"⚠️ Agent tick failed: {str(e)[:300]}")
                last_alert = time.time()
        try:
            time.sleep(cfg.poll_minutes * 60)
        except KeyboardInterrupt:
            log.info("Stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
