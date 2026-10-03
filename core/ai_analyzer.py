"""Reply intent analysis. Cheap rules first, LLM second, rules again if LLM fails."""
import json
import re

import requests

from templates.prompts import (
    ANALYZE_SYSTEM_PROMPT,
    ANALYZE_USER_TEMPLATE,
    INBOX_BN_SYSTEM_PROMPT,
    INBOX_BN_USER_TEMPLATE,
)
from utils.helpers import looks_like_unsubscribe, snippet
from utils.logger import get_logger

log = get_logger("agent.ai")

INTENTS = {"interested", "question", "referral", "not_interested", "unsubscribe", "other"}
NOT_INT_RE = re.compile(r"\b(not interested|no thanks|no thank you|not a fit|not for us|we('| a)re (all set|good)|already (have|use|work))\b", re.I)
POSITIVE_RE = re.compile(r"\b(interested|interesting|sounds good|sounds great|let'?s (talk|chat|connect|schedule)|book a call|send (me )?(more|details|info|pricing)|tell me more|yes\b|sure\b|happy to chat|free (on|this|next))\b", re.I)
QUESTION_RE = re.compile(r"\?|how much|what('s| is| do)|pricing|price|cost", re.I)
REFERRAL_RE = re.compile(r"\b(contact|reach out to|speak (to|with)|talk to|forwarded|cc'?d?)\b", re.I)


def rule_based(text: str) -> dict:
    t = text or ""
    if looks_like_unsubscribe(t):
        intent = "unsubscribe"
    elif NOT_INT_RE.search(t):
        intent = "not_interested"
    elif POSITIVE_RE.search(t):
        intent = "interested"
    elif REFERRAL_RE.search(t):
        intent = "referral"
    elif QUESTION_RE.search(t):
        intent = "question"
    else:
        intent = "other"
    return {"intent": intent, "summary": snippet(t, 120), "suggested_reply": "", "source": "rules"}


def _candidate_models(primary: str) -> list:
    name = (primary or "gemini-3.5-flash").replace("models/", "")
    models = [name]
    for alt in ["gemini-3.5-flash", "gemini-flash-lite-latest", "gemini-3.8-flash"]:
        if alt not in models:
            models.append(alt)
    return models


class AIAnalyzer:
    def __init__(self, cfg):
        self.key = getattr(cfg, "gemini_api_key", "") or getattr(cfg, "anthropic_api_key", "")
        self.model = getattr(cfg, "gemini_model", "") or getattr(cfg, "anthropic_model", "gemini-3.5-flash")

    def analyze(self, body: str, name: str = "", company: str = "") -> dict:
        body = (body or "").strip()
        if not body:
            return {"intent": "other", "summary": "(empty reply)", "suggested_reply": "", "source": "rules"}
        # Unsubscribe is decided by rules: never risk a second mail to someone who said stop.
        if looks_like_unsubscribe(body):
            return rule_based(body)
        if not self.key:
            return rule_based(body)
        try:
            return self._llm(body, name, company)
        except Exception as e:
            log.warning("LLM analysis failed, using rules: %s", e)
            return rule_based(body)

    def _llm(self, body, name, company) -> dict:
        last_err = None
        for model_name in _candidate_models(self.model):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{
                            "text": ANALYZE_USER_TEMPLATE.format(
                                name=name or "unknown",
                                company=company or "unknown",
                                body=body[:3000],
                            )
                        }],
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": ANALYZE_SYSTEM_PROMPT}]
                },
                "generationConfig": {
                    "maxOutputTokens": 350,
                    "responseMimeType": "application/json",
                },
            }
            try:
                r = requests.post(
                    url,
                    headers={"Content-Type": "application/json", "x-goog-api-key": self.key},
                    json=payload,
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if "text" in p)
                return self.parse_llm_output(text)
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise ValueError("No LLM candidate returned output")

    @staticmethod
    def parse_llm_output(text: str) -> dict:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            raise ValueError("no JSON in LLM output")
        data = json.loads(m.group(0))
        intent = str(data.get("intent", "other")).strip().lower()
        if intent not in INTENTS:
            intent = "other"
        return {
            "intent": intent,
            "summary": snippet(str(data.get("summary", "")), 160),
            "suggested_reply": str(data.get("suggested_reply", ""))[:600],
            "source": "llm",
        }

    def analyze_email_bn(self, from_raw: str, subject: str, body: str) -> dict:
        """Analyze an incoming inbox email with Gemini and return rich Bengali summary/actions."""
        b = (body or "").strip()
        if not b:
            return {
                "summary_bn": "ইমেইলে কোনো বার্তা পাওয়া যায়নি (সম্ভবত কেবল অ্যাটাচমেন্ট)।",
                "intent_bn": "খালি ইমেইল বা অ্যাটাচমেন্ট",
                "action_bn": "ইনবক্সে গিয়ে ইমেইলটি ও অ্যাটাচমেন্ট চেক করুন।",
                "suggested_reply": "",
            }
        if not self.key:
            return {
                "summary_bn": snippet(b, 200),
                "intent_bn": "সাধারণ বার্তা",
                "action_bn": "ইনবক্স চেক করে প্রয়োজনীয় ব্যবস্থা নিন।",
                "suggested_reply": "",
            }
        for model_name in _candidate_models(self.model):
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
                user_content = INBOX_BN_USER_TEMPLATE.format(
                    sender=from_raw or "অজানা",
                    subject=subject or "কোন বিষয় নেই",
                    body=b[:4000],
                )
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": user_content}]}],
                    "systemInstruction": {"parts": [{"text": INBOX_BN_SYSTEM_PROMPT}]},
                    "generationConfig": {"maxOutputTokens": 600, "responseMimeType": "application/json"},
                }
                r = requests.post(
                    url,
                    headers={"Content-Type": "application/json", "x-goog-api-key": self.key},
                    json=payload,
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts if "text" in p)
                    m = re.search(r"\{.*\}", text or "", re.S)
                    if m:
                        res = json.loads(m.group(0))
                        is_work = res.get("is_work_or_human", True)
                        if isinstance(is_work, str):
                            is_work = is_work.lower() in ("true", "1", "yes")
                        return {
                            "is_work_or_human": bool(is_work),
                            "summary_bn": str(res.get("summary_bn", "")).strip(),
                            "intent_bn": str(res.get("intent_bn", "")).strip() or "সাধারণ বার্তা",
                            "action_bn": str(res.get("action_bn", "")).strip() or "ইনবক্স চেক করুন।",
                            "suggested_reply": str(res.get("suggested_reply", "")).strip(),
                        }
            except Exception as e:
                log.warning("Gemini model %s failed, trying fallback: %s", model_name, e)
                continue

        return {
            "summary_bn": snippet(b, 200),
            "intent_bn": "সাধারণ বার্তা",
            "action_bn": "ইনবক্স চেক করুন।",
            "suggested_reply": "",
        }
