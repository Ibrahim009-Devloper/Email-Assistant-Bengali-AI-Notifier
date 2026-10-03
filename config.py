"""Config loader. All secrets/settings come from .env."""
import os
from dataclasses import dataclass
from typing import List, Tuple

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool(v: str, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _days(v, default):
    try:
        out = tuple(int(x) for x in str(v).split(",") if x.strip())
        return out or default
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    sheet_id: str = ""
    sheet_tab: str = "Leads"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    sender_name: str = ""
    sender_address: str = ""
    offer_line: str = ""
    daily_limit: int = 30
    per_run_limit: int = 8
    warmup_start: int = 10
    warmup_step: int = 5
    delay_min: int = 60
    delay_max: int = 180
    followup_days: Tuple[int, ...] = (3, 7)
    no_reply_after_days: int = 4
    send_tz: str = "America/New_York"
    send_start_hour: int = 9
    send_end_hour: int = 16
    weekdays_only: bool = True
    poll_minutes: int = 20
    bounce_pause_rate: float = 0.05
    bounce_min_sample: int = 20
    bounce_lookback_days: int = 7
    dry_run: bool = False
    template_path: str = os.path.join(BASE_DIR, "templates", "email_body.html")
    log_dir: str = os.path.join(BASE_DIR, "logs")
    lock_path: str = os.path.join(BASE_DIR, "data", "agent.lock")


def _path(v: str) -> str:
    return v if os.path.isabs(v) else os.path.join(BASE_DIR, v)


def load_config() -> Config:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    g = os.getenv
    d = Config()
    return Config(
        google_credentials_file=_path(g("GOOGLE_CREDENTIALS_FILE", d.google_credentials_file)),
        google_token_file=_path(g("GOOGLE_TOKEN_FILE", d.google_token_file)),
        sheet_id=g("SHEET_ID", ""),
        sheet_tab=g("SHEET_TAB", d.sheet_tab),
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=g("TELEGRAM_CHAT_ID", ""),
        gemini_api_key=g("GEMINI_API_KEY") or g("ANTHROPIC_API_KEY", ""),
        gemini_model=g("GEMINI_MODEL") or g("ANTHROPIC_MODEL", d.gemini_model),
        anthropic_api_key=g("ANTHROPIC_API_KEY", ""),
        anthropic_model=g("ANTHROPIC_MODEL", ""),
        sender_name=g("SENDER_NAME", ""),
        sender_address=g("SENDER_ADDRESS", ""),
        offer_line=g("OFFER_LINE", ""),
        daily_limit=_int(g("DAILY_LIMIT"), d.daily_limit),
        per_run_limit=_int(g("PER_RUN_LIMIT"), d.per_run_limit),
        warmup_start=_int(g("WARMUP_START"), d.warmup_start),
        warmup_step=_int(g("WARMUP_STEP"), d.warmup_step),
        delay_min=_int(g("DELAY_MIN"), d.delay_min),
        delay_max=_int(g("DELAY_MAX"), d.delay_max),
        followup_days=_days(g("FOLLOWUP_DAYS"), d.followup_days),
        no_reply_after_days=_int(g("NO_REPLY_AFTER_DAYS"), d.no_reply_after_days),
        send_tz=g("SEND_TZ", d.send_tz),
        send_start_hour=_int(g("SEND_START_HOUR"), d.send_start_hour),
        send_end_hour=_int(g("SEND_END_HOUR"), d.send_end_hour),
        weekdays_only=_bool(g("WEEKDAYS_ONLY"), d.weekdays_only),
        poll_minutes=_int(g("POLL_MINUTES"), d.poll_minutes),
        bounce_pause_rate=_float(g("BOUNCE_PAUSE_RATE"), d.bounce_pause_rate),
        bounce_min_sample=_int(g("BOUNCE_MIN_SAMPLE"), d.bounce_min_sample),
        dry_run=_bool(g("DRY_RUN"), d.dry_run),
    )


def validate(cfg: Config, for_send: bool = True) -> List[str]:
    """Return list of config problems (empty = OK)."""
    p = []
    if for_send:
        if not cfg.sheet_id or cfg.sheet_id.startswith("PASTE"):
            p.append("SHEET_ID missing")
        if not cfg.sender_name:
            p.append("SENDER_NAME missing")
        if not cfg.sender_address:
            p.append("SENDER_ADDRESS missing (postal address required for cold email)")
        if not cfg.offer_line:
            p.append("OFFER_LINE missing")
        if cfg.delay_min > cfg.delay_max:
            p.append("DELAY_MIN > DELAY_MAX")
    if not os.path.exists(cfg.google_credentials_file) and not os.path.exists(cfg.google_token_file):
        p.append(f"Google credentials/token file not found: {cfg.google_credentials_file}")
    return p
