"""Google Sheets as the database. Header-name based, tolerant to your Gemini sheet's column names."""
import re

from utils.logger import get_logger

log = get_logger("agent.sheets")

COLUMNS = [
    "email", "name", "company", "hook", "status", "thread_id", "rfc_message_id", "subject",
    "sent_at", "last_sent_at", "followups", "last_reply_at", "reply_intent", "reply_snippet", "notes",
]

# Gemini sheet header -> internal name
ALIASES = {
    "owner email": "email", "email address": "email", "owner name": "name", "full name": "name",
    "agency": "company", "agency name": "company", "company name": "company",
    "personalization hook": "hook", "personalisation hook": "hook",
}


def canon(header: str) -> str:
    k = re.sub(r"\s+", " ", (header or "").strip().lower())
    return ALIASES.get(k, k.replace(" ", "_"))


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class SheetsService:
    def __init__(self, ws, dash_factory=None):
        self.ws = ws
        self.dash_factory = dash_factory
        self._cols = {}  # canonical name -> 0-based index

    @classmethod
    def connect(cls, creds, cfg):
        import gspread

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(cfg.sheet_id)
        ws = sh.worksheet(cfg.sheet_tab)

        def dash():
            try:
                return sh.worksheet("Dashboard")
            except gspread.WorksheetNotFound:
                return sh.add_worksheet("Dashboard", rows=60, cols=4)

        svc = cls(ws, dash)
        svc.ensure_schema()
        return svc

    # ---- schema ----
    def ensure_schema(self):
        values = self.ws.get_all_values()
        header = list(values[0]) if values else []
        header = [h for h in header]
        names = [canon(h) for h in header]
        if not any(header):
            header, names = [], []
        missing = [c for c in COLUMNS if c not in names]
        if missing:
            start = len(header)
            need = start + len(missing)
            if getattr(self.ws, "col_count", need) < need:
                self.ws.add_cols(need - self.ws.col_count)
            rng = f"{col_letter(start + 1)}1:{col_letter(need)}1"
            self.ws.batch_update([{"range": rng, "values": [missing]}], value_input_option="RAW")
            header += missing
            names += missing
            log.info("Added columns: %s", missing)
        self._cols = {}
        for i, n in enumerate(names):
            self._cols.setdefault(n, i)

    # ---- read ----
    def get_rows(self) -> list:
        values = self.ws.get_all_values()
        if not values:
            return []
        names = [canon(h) for h in values[0]]
        rows = []
        for i, row in enumerate(values[1:], start=2):
            d = {}
            for j, n in enumerate(names):
                d.setdefault(n, row[j].strip() if j < len(row) else "")
            if not d.get("email"):
                continue
            d["_row"] = i
            rows.append(d)
        return rows

    # ---- write ----
    def update(self, row_num: int, fields: dict):
        data = []
        for k, v in fields.items():
            if k not in self._cols:
                continue
            a1 = f"{col_letter(self._cols[k] + 1)}{row_num}"
            data.append({"range": a1, "values": [["" if v is None else str(v)]]})
        if data:
            self.ws.batch_update(data, value_input_option="RAW")

    def write_dashboard(self, stats: dict):
        if not self.dash_factory:
            return
        c = stats["counts"]
        rows = [
            ["Metric", "Value"],
            ["Total leads", stats["total"]],
            ["Emails sent (first touch)", stats["sent"]],
            ["Replied", c.get("replied", 0)],
            ["Not interested", c.get("not_interested", 0)],
            ["Unsubscribed", c.get("unsubscribed", 0)],
            ["Bounced", c.get("bounced", 0)],
            ["No reply (sequence finished)", c.get("no_reply", 0)],
            ["In sequence (sent/followup)", sum(c.get(s, 0) for s in ("sent", "followup1", "followup2"))],
            ["Not yet sent", c.get("new", 0)],
            ["Reply rate (any reply)", f"{stats['reply_rate']:.1%}"],
            ["Positive reply rate", f"{stats['positive_rate']:.1%}"],
            ["Bounce rate", f"{stats['bounce_rate']:.1%}"],
        ]
        ws = self.dash_factory()
        ws.clear()
        ws.batch_update([{"range": f"A1:B{len(rows)}", "values": rows}], value_input_option="RAW")
