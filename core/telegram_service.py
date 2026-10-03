"""Telegram alerts. Never raises: alerts must not break the pipeline."""
import requests

from utils.logger import get_logger

log = get_logger("agent.telegram")


class TelegramService:
    def __init__(self, cfg):
        self.token = cfg.telegram_bot_token
        self.chat_id = cfg.telegram_chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[telegram disabled] %s", text.replace("\n", " | ")[:200])
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("Telegram failed: %s", e)
            return False
