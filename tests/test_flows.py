"""Offline tests: fakes replace Gmail/Sheets/Telegram. Run: python -m unittest discover -s tests -v"""
import base64
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from core.ai_analyzer import AIAnalyzer, rule_based  # noqa: E402
from core.gmail_service import (PermanentSendError, QuotaError, build_mime, parse_message, SendError)  # noqa: E402
from core.sheets_service import COLUMNS, SheetsService, canon, col_letter  # noqa: E402
from utils import helpers as H  # noqa: E402
from workflows import outreach_flow, reply_monitor_flow  # noqa: E402

ME = "me@sender.com"
NOW = datetime(2026, 10, 6, 15, 0, 0, tzinfo=timezone.utc)


def ms(dt):
    return int(dt.timestamp() * 1000)


def mk_msg(i, frm, body="", subject="", headers=None, when=None, mid=None, thread="t1"):
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return {"id": i, "thread_id": thread, "from_raw": frm, "from_addr": frm.lower(), "subject": subject,
            "message_id": mid or f"<{i}@x>", "headers": h, "body": body, "date_ms": ms(when or NOW), "labels": []}


class FakeGmail:
    me = ME

    def __init__(self):
        self.threads, self.sent, self.bounces, self.n = {}, [], [], 0
        self.raise_on_send = None
        self.fail_thread = False

    def send(self, to, subject, text, html, thread_id=None, in_reply_to=None, references=None):
        if self.raise_on_send:
            raise self.raise_on_send
        self.n += 1
        tid = thread_id or f"th{self.n}"
        mid = f"<sent{self.n}@sender>"
        self.sent.append(dict(to=to, subject=subject, text=text, html=html, thread_id=tid,
                              in_reply_to=in_reply_to, references=references))
        self.threads.setdefault(tid, []).append(mk_msg(f"s{self.n}", ME, text, subject, mid=mid, thread=tid, when=NOW))
        return {"id": f"s{self.n}", "thread_id": tid, "message_id": mid}

    def get_thread(self, tid):
        if self.fail_thread:
            raise RuntimeError("boom")
        return list(self.threads.get(tid, []))

    def search_bounces(self, days=7):
        return list(self.bounces)


class FakeSheets:
    def __init__(self, rows):
        self.rows = [dict(r, _row=i + 2) for i, r in enumerate(rows)]
        self.dash = None
        self.fail_update = False

    def get_rows(self):
        return [dict(r) for r in self.rows]

    def update(self, n, fields):
        if self.fail_update:
            raise RuntimeError("sheet down")
        for r in self.rows:
            if r["_row"] == n:
                r.update({k: ("" if v is None else str(v)) for k, v in fields.items()})

    def write_dashboard(self, stats):
        self.dash = stats

    def row(self, email):
        return next(r for r in self.rows if r["email"] == email)


class FakeTG:
    def __init__(self):
        self.msgs = []

    def send(self, t):
        self.msgs.append(t)
        return True


def cfg(**kw):
    base = dict(sender_name="Alex Doe", sender_address="1 Main St, Austin TX", offer_line="We add 20% retainer revenue for email agencies.",
                per_run_limit=8, daily_limit=30, warmup_start=10, warmup_step=5, delay_min=1, delay_max=2)
    base.update(kw)
    return Config(**base)


def lead(email, **kw):
    d = {"email": email, "name": "Jane Roe", "company": "Acme Mail", "hook": "Loved your Klaviyo flows case study", "status": ""}
    d.update(kw)
    return d


def ago(days, extra_h=1):
    return H.iso(NOW - timedelta(days=days, hours=extra_h))


class OutreachTests(unittest.TestCase):
    def setUp(self):
        self.g, self.t, self.sleeps = FakeGmail(), FakeTG(), []

    def go(self, rows, **ck):
        self.s = FakeSheets(rows)
        self.res = outreach_flow.run(cfg(**ck), self.g, self.s, self.t, sleep=self.sleeps.append, now=NOW)
        return self.res

    def test_sends_new_logs_and_filters_bad(self):
        self.go([lead("jane@acme.com"), lead("info@acme.com"), lead("not-an-email"), lead("jane@acme.com", name="dup")])
        r = self.s.row("jane@acme.com")
        self.assertEqual(self.res["sent"], 1)
        self.assertEqual(r["status"], "sent")
        self.assertTrue(r["thread_id"] and r["rfc_message_id"] and r["sent_at"] and r["subject"])
        self.assertEqual(self.s.row("info@acme.com")["status"], "skipped")
        self.assertEqual(self.s.row("not-an-email")["status"], "invalid")
        self.assertEqual(len([x for x in self.s.rows if x["status"] == "skipped"]), 2)  # generic + duplicate
        mail = self.g.sent[0]
        self.assertIn("Hi Jane", mail["text"])
        self.assertIn("Klaviyo flows", mail["text"])
        self.assertIn("1 Main St", mail["text"])
        self.assertIn('reply "stop"', mail["text"].lower())
        self.assertNotIn("{{", mail["text"] + mail["html"] + mail["subject"])
        self.assertTrue(mail["subject"].startswith("quick question, Jane"))

    def test_per_run_limit_and_delay(self):
        self.go([lead(f"p{i}@acme.com") for i in range(10)], per_run_limit=3)
        self.assertEqual(self.res["sent"], 3)
        self.assertEqual(len(self.sleeps), 2)  # between sends, not after last

    def test_warmup_cap_across_runs(self):
        rows = [lead(f"p{i}@acme.com") for i in range(10)]
        self.go(rows, per_run_limit=3, warmup_start=4)
        self.assertEqual(self.res["sent"], 3)
        res2 = outreach_flow.run(cfg(per_run_limit=3, warmup_start=4), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(res2["sent"], 1)  # cap 4/day reached
        res3 = outreach_flow.run(cfg(per_run_limit=3, warmup_start=4), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(res3["sent"], 0)

    def test_followup_timing_and_threading(self):
        self.go([lead("a@acme.com")], per_run_limit=1)
        tid = self.s.row("a@acme.com")["thread_id"]
        # 2 days later: nothing
        self.s.update(2, {"sent_at": ago(2)})
        r = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(r["followups"], 0)
        # 3+ days later: follow-up 1 in same thread
        self.s.update(2, {"sent_at": ago(3), "last_sent_at": ago(3)})
        r = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(r["followups"], 1)
        fu = self.g.sent[-1]
        self.assertEqual(fu["thread_id"], tid)
        self.assertTrue(fu["subject"].startswith("Re: quick question"))
        self.assertTrue(fu["in_reply_to"])
        self.assertEqual(self.s.row("a@acme.com")["status"], "followup1")
        # 7+ days: follow-up 2
        self.s.update(2, {"sent_at": ago(7), "last_sent_at": ago(4)})
        r = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(r["followups"], 1)
        self.assertEqual(self.s.row("a@acme.com")["status"], "followup2")
        # sequence finished -> no more mail, later closes as no_reply
        self.s.update(2, {"sent_at": ago(12)})
        n = len(self.g.sent)
        r = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(len(self.g.sent), n)
        self.assertEqual(self.s.row("a@acme.com")["status"], "no_reply")

    def test_followup_blocked_if_thread_has_reply(self):
        self.go([lead("a@acme.com")], per_run_limit=1)
        tid = self.s.row("a@acme.com")["thread_id"]
        self.g.threads[tid].append(mk_msg("r1", "a@acme.com", "Hi there", thread=tid))
        self.s.update(2, {"sent_at": ago(5)})
        n = len(self.g.sent)
        r = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(len(self.g.sent), n)
        self.assertEqual(self.s.row("a@acme.com")["status"], "sent")

    def test_followup_skipped_when_thread_fetch_fails(self):
        self.go([lead("a@acme.com")], per_run_limit=1)
        self.s.update(2, {"sent_at": ago(5)})
        self.g.fail_thread = True
        n = len(self.g.sent)
        outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(len(self.g.sent), n)

    def test_terminal_statuses_never_mailed(self):
        rows = [lead(f"{s}@acme.com", status=s, thread_id="x", sent_at=ago(30)) for s in
                ("replied", "unsubscribed", "bounced", "not_interested", "no_reply", "invalid", "skipped")]
        self.go(rows)
        self.assertEqual(self.g.sent, [])

    def test_dry_run_changes_nothing(self):
        self.go([lead("a@acme.com")], dry_run=True)
        self.assertEqual(self.g.sent, [])
        self.assertEqual(self.s.row("a@acme.com")["status"], "")

    def test_bounce_rate_pause(self):
        rows = [lead(f"b{i}@acme.com", status="bounced", sent_at=ago(2)) for i in range(3)]
        rows += [lead(f"s{i}@acme.com", status="sent", sent_at=ago(2), thread_id="x") for i in range(27)]
        rows += [lead("new@acme.com")]
        self.go(rows, bounce_min_sample=20, bounce_pause_rate=0.05)
        self.assertEqual(self.res["aborted"], "bounce_rate")
        self.assertEqual(self.g.sent, [])
        self.assertTrue(any("paused" in m.lower() for m in self.t.msgs))

    def test_quota_aborts(self):
        self.g.raise_on_send = QuotaError("limit", 429)
        self.go([lead("a@acme.com"), lead("b@acme.com")])
        self.assertEqual(self.res["aborted"], "quota")
        self.assertEqual(self.s.row("a@acme.com")["status"], "")

    def test_permanent_error_marks_invalid(self):
        self.g.raise_on_send = PermanentSendError("bad", 400)
        self.go([lead("a@acme.com")])
        self.assertEqual(self.s.row("a@acme.com")["status"], "invalid")

    def test_three_transient_failures_abort(self):
        self.g.raise_on_send = SendError("net")
        self.go([lead(f"x{i}@acme.com") for i in range(6)])
        self.assertEqual(self.res["aborted"], "send_failures")
        self.assertEqual(self.res["errors"], 3)

    def test_sheet_write_failure_stops_run(self):
        self.s = FakeSheets([lead("a@acme.com"), lead("b@acme.com")])
        orig = self.s.update
        self.s.fail_update = False

        def flaky(n, f):
            if f.get("status") == "sent":
                raise RuntimeError("down")
            return orig(n, f)
        self.s.update = flaky
        res = outreach_flow.run(cfg(), self.g, self.s, self.t, sleep=lambda x: None, now=NOW)
        self.assertEqual(res["aborted"], "sheet_write")
        self.assertEqual(len(self.g.sent), 1)  # never second mail
        self.assertTrue(any("FAILED" in m for m in self.t.msgs))


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.g, self.t = FakeGmail(), FakeTG()
        self.an = AIAnalyzer(cfg())  # no key => rules

    def seed(self, inbound, extra=None):
        rows = [lead("a@acme.com", status="sent", thread_id="t1", sent_at=ago(2))]
        self.s = FakeSheets(rows)
        self.g.threads["t1"] = [mk_msg("s1", ME, 'Hi\n\nReply "stop" to opt out', "quick question")]
        if inbound:
            self.g.threads["t1"].append(inbound)

    def go(self):
        return reply_monitor_flow.run(cfg(), self.g, self.s, self.t, self.an)

    def test_interested_reply(self):
        self.seed(mk_msg("r1", "a@acme.com", "Sure, let's talk Tuesday!\n\nOn Mon, Alex wrote:\n> Reply \"stop\" to opt out",
                         when=NOW + timedelta(minutes=5)))
        r = self.go()
        row = self.s.row("a@acme.com")
        self.assertEqual(row["status"], "replied")
        self.assertEqual(row["reply_intent"], "interested")
        self.assertIn("Tuesday", row["reply_snippet"])
        self.assertNotIn("stop", row["reply_snippet"].lower())
        self.assertEqual(r["replied"], 1)
        self.assertEqual(len(self.t.msgs), 1)
        self.assertIn("Reply from", self.t.msgs[0])

    def test_quoted_footer_not_misread_as_unsubscribe(self):
        self.seed(mk_msg("r1", "a@acme.com", 'Thanks, sounds interesting.\n\n> If not relevant reply "stop" to opt out',
                         when=NOW + timedelta(minutes=5)))
        self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "replied")

    def test_unsubscribe(self):
        self.seed(mk_msg("r1", "a@acme.com", "stop", when=NOW + timedelta(minutes=5)))
        self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "unsubscribed")
        self.seed(mk_msg("r1", "a@acme.com", "Please stop emailing me.", when=NOW + timedelta(minutes=5)))
        self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "unsubscribed")

    def test_not_interested(self):
        self.seed(mk_msg("r1", "a@acme.com", "No thanks, we already use another agency.", when=NOW + timedelta(minutes=5)))
        self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "not_interested")

    def test_ooo_does_not_change_status_and_is_not_reprocessed(self):
        self.seed(mk_msg("r1", "a@acme.com", "I am away until Monday", "Automatic reply: quick question",
                         headers={"Auto-Submitted": "auto-replied"}, when=NOW + timedelta(minutes=5)))
        r = self.go()
        row = self.s.row("a@acme.com")
        self.assertEqual(row["status"], "sent")
        self.assertIn("OOO", row["notes"])
        self.assertEqual(r["ooo"], 1)
        self.assertEqual(self.t.msgs, [])
        r2 = self.go()
        self.assertEqual(r2["ooo"], 0)  # idempotent

    def test_real_reply_after_ooo(self):
        self.seed(mk_msg("r1", "a@acme.com", "away", "Out of office", headers={"Auto-Submitted": "auto-replied"},
                         when=NOW + timedelta(minutes=5)))
        self.go()
        self.g.threads["t1"].append(mk_msg("r2", "a@acme.com", "Back now, send pricing please", when=NOW + timedelta(days=3)))
        self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "replied")

    def test_bounce_in_thread(self):
        self.seed(mk_msg("r1", "mailer-daemon@googlemail.com", "Address not found a@acme.com", "Delivery Status Notification (Failure)",
                         when=NOW + timedelta(minutes=1)))
        r = self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "bounced")
        self.assertEqual(r["bounced"], 1)

    def test_bounce_sweep_outside_thread(self):
        self.seed(None)
        self.g.bounces = [mk_msg("b1", "mailer-daemon@googlemail.com", "550 user unknown: a@acme.com", thread="other",
                                 headers={"X-Failed-Recipients": "a@acme.com"})]
        r = self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "bounced")
        self.assertEqual(r["bounced"], 1)
        r2 = self.go()
        self.assertEqual(r2["bounced"], 0)

    def test_no_reply_nothing_happens(self):
        self.seed(None)
        r = self.go()
        self.assertEqual(self.s.row("a@acme.com")["status"], "sent")
        self.assertEqual(r["checked"], 1)

    def test_thread_error_is_survivable(self):
        self.seed(None)
        self.g.fail_thread = True
        r = self.go()
        self.assertEqual(r["errors"], 1)


class HelperTests(unittest.TestCase):
    def test_email_checks(self):
        self.assertTrue(H.is_valid_email("a.b+c@sub.example.co"))
        for bad in ("", "a@b", "a@@b.com", "a b@c.com", "a..b@c.com"):
            self.assertFalse(H.is_valid_email(bad), bad)
        self.assertTrue(H.is_generic_email("hello@x.com"))
        self.assertFalse(H.is_generic_email("amanda@x.com"))

    def test_first_name(self):
        self.assertEqual(H.first_name("Dr. amanda kern"), "Amanda")
        self.assertEqual(H.first_name(""), "there")
        self.assertEqual(H.first_name("McKenzie Lee"), "McKenzie")

    def test_strip_quoted(self):
        t = "Yes please.\n\nOn Tue, Oct 6, 2026 at 3:00 PM Alex Doe <me@sender.com>\nwrote:\n> hi"
        self.assertEqual(H.strip_quoted(t), "Yes please.")
        self.assertEqual(H.strip_quoted("ok\n> quoted\nmore"), "ok\nmore")

    def test_unsub_detection(self):
        for t in ("stop", "STOP.", "unsubscribe me", "please remove me from your list", "Don't contact me again"):
            self.assertTrue(H.looks_like_unsubscribe(t), t)
        for t in ("Let's stop by next week to discuss", "sounds great", "stopwatch company"):
            self.assertFalse(H.looks_like_unsubscribe(t), t)

    def test_stats(self):
        rows = [{"status": "sent", "sent_at": "x"}, {"status": "replied", "sent_at": "x"}, {"status": "bounced", "sent_at": "x"}, {"status": ""}]
        s = H.compute_stats(rows)
        self.assertEqual((s["sent"], s["replied"], s["bounced"]), (3, 1, 1))
        self.assertAlmostEqual(s["bounce_rate"], 1 / 3)

    def test_template_sections_render(self):
        sec = H.load_sections(Config().template_path)
        self.assertEqual(set(sec), {"initial", "followup1", "followup2"})
        self.assertIsNotNone(sec["initial"]["subject"])
        self.assertIsNone(sec["followup1"]["subject"])

    def test_lock(self):
        p = "/tmp/_agent_test.lock"
        with H.file_lock(p):
            with self.assertRaises(H.LockBusy):
                with H.file_lock(p):
                    pass
        with H.file_lock(p):
            pass

    def test_is_promotional_or_automated(self):
        # Promotion label
        self.assertTrue(H.is_promotional_or_automated({"labels": ["CATEGORY_PROMOTIONS"]}))
        # Updates label
        self.assertTrue(H.is_promotional_or_automated({"labels": ["CATEGORY_UPDATES"]}))
        # List-unsubscribe header
        self.assertTrue(H.is_promotional_or_automated({"headers": {"list-unsubscribe": "<mailto:unsub@x>"}}))
        # Bot sender
        self.assertTrue(H.is_promotional_or_automated({"from_addr": "no-reply@accounts.google.com"}))
        self.assertTrue(H.is_promotional_or_automated({"from_addr": "newsletter@company.com"}))
        # Genuine human email
        self.assertFalse(H.is_promotional_or_automated({"labels": ["INBOX", "UNREAD"], "from_addr": "john.smith@gmail.com", "headers": {}}))


class GmailParseTests(unittest.TestCase):
    def b64(self, s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    def test_parse_multipart(self):
        raw = {"id": "1", "threadId": "t", "internalDate": "1760000000000", "labelIds": ["INBOX"], "payload": {
            "headers": [{"name": "From", "value": "Jane Roe <Jane@Acme.com>"}, {"name": "Subject", "value": "Re: hi"},
                        {"name": "Message-ID", "value": "<m1@acme>"}],
            "mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": self.b64("Hello plain")}},
                {"mimeType": "text/html", "body": {"data": self.b64("<p>Hello html</p>")}}]}}
        m = parse_message(raw)
        self.assertEqual(m["from_addr"], "jane@acme.com")
        self.assertEqual(m["body"], "Hello plain")
        self.assertEqual(m["date_ms"], 1760000000000)

    def test_parse_html_only(self):
        raw = {"id": "1", "payload": {"headers": [], "mimeType": "text/html", "body": {"data": self.b64("<p>Hi<br>there</p>")}}}
        self.assertEqual(parse_message(raw)["body"], "Hi\nthere")

    def test_mime_headers(self):
        m = build_mime(ME, "Alex Doe", "a@acme.com", "Re: x", "plain", "<p>plain</p>", "<a@b>", "<a@b> <c@d>")
        self.assertEqual(m["In-Reply-To"], "<a@b>")
        self.assertEqual(m["References"], "<a@b> <c@d>")
        self.assertIn("List-Unsubscribe", m)
        self.assertTrue(m.is_multipart())


class AITests(unittest.TestCase):
    def test_llm_output_parsing(self):
        out = AIAnalyzer.parse_llm_output('Here: {"intent":"Interested","summary":"wants call","suggested_reply":"Great!"}')
        self.assertEqual(out["intent"], "interested")
        self.assertEqual(AIAnalyzer.parse_llm_output('{"intent":"weird"}')["intent"], "other")
        with self.assertRaises(ValueError):
            AIAnalyzer.parse_llm_output("no json")

    def test_llm_failure_falls_back_to_rules(self):
        a = AIAnalyzer(cfg(gemini_api_key="k"))
        a._llm = lambda *x: (_ for _ in ()).throw(RuntimeError("api down"))
        self.assertEqual(a.analyze("Sure, send me details")["intent"], "interested")

    def test_unsubscribe_never_goes_to_llm(self):
        a = AIAnalyzer(cfg(gemini_api_key="k"))
        a._llm = lambda *x: (_ for _ in ()).throw(AssertionError("LLM must not be called"))
        self.assertEqual(a.analyze("remove me from your list")["intent"], "unsubscribe")

    def test_gemini_llm_response_parsing(self):
        from unittest.mock import patch, MagicMock
        a = AIAnalyzer(cfg(gemini_api_key="test-key", gemini_model="gemini-2.5-flash"))
        fake_response = MagicMock()
        fake_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"intent": "interested", "summary": "Wants info", "suggested_reply": "Thanks!"}'}
                        ]
                    }
                }
            ]
        }
        fake_response.raise_for_status = MagicMock()
        with patch("requests.post", return_value=fake_response) as mock_post:
            res = a.analyze("Can you send more details?", "Jane", "Acme")
            self.assertEqual(res["intent"], "interested")
            self.assertEqual(res["source"], "llm")
            self.assertEqual(res["suggested_reply"], "Thanks!")
            # verify request went to Gemini endpoint with proper header
            self.assertTrue(mock_post.called)
            args, kwargs = mock_post.call_args
            self.assertIn("generativelanguage.googleapis.com", args[0])
            self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")

    def test_rules(self):
        self.assertEqual(rule_based("What's the pricing?")["intent"], "question")
        self.assertEqual(rule_based("Please contact my colleague Sam")["intent"], "referral")


class FakeWS:
    col_count = 26

    def __init__(self, values):
        self.values = values
        self.calls = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def add_cols(self, n):
        self.col_count += n

    def batch_update(self, data, value_input_option=None):
        for d in data:
            self.calls.append(d)
            cell = d["range"].split(":")[0]
            col = 0
            i = 0
            while cell[i].isalpha():
                col = col * 26 + ord(cell[i]) - 64
                i += 1
            row = int(cell[i:])
            for dr, vals in enumerate(d["values"]):
                while len(self.values) < row + dr:
                    self.values.append([])
                line = self.values[row - 1 + dr]
                for dc, v in enumerate(vals):
                    while len(line) < col + dc:
                        line.append("")
                    line[col - 1 + dc] = v

    def clear(self):
        self.values = []


class SheetsTests(unittest.TestCase):
    def test_col_letter(self):
        self.assertEqual([col_letter(1), col_letter(26), col_letter(27), col_letter(52)], ["A", "Z", "AA", "AZ"])

    def test_aliases_and_schema_extension(self):
        ws = FakeWS([["Rank", "Agency", "Owner Name", "Owner Email", "Personalization Hook"],
                     ["1", "Acme Mail", "Jane Roe", "Jane@acme.com", "nice post"],
                     ["2", "NoMail Co", "Bob", "", "x"]])
        s = SheetsService(ws)
        s.ensure_schema()
        rows = s.get_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["company"], rows[0]["name"], rows[0]["email"], rows[0]["hook"]),
                         ("Acme Mail", "Jane Roe", "Jane@acme.com", "nice post"))
        s.update(rows[0]["_row"], {"status": "sent", "thread_id": "t9"})
        again = s.get_rows()[0]
        self.assertEqual((again["status"], again["thread_id"]), ("sent", "t9"))
        header = ws.values[0]
        self.assertEqual(header[:5], ["Rank", "Agency", "Owner Name", "Owner Email", "Personalization Hook"])
        for c in ("status", "thread_id", "sent_at", "notes"):
            self.assertIn(c, header)
        self.assertEqual(canon("Owner Email"), "email")

    def test_empty_sheet_gets_headers(self):
        ws = FakeWS([])
        s = SheetsService(ws)
        s.ensure_schema()
        self.assertEqual(ws.values[0], COLUMNS)

    def test_dashboard(self):
        ws, dash = FakeWS([COLUMNS]), FakeWS([])
        s = SheetsService(ws, lambda: dash)
        s.write_dashboard(H.compute_stats([{"status": "sent", "sent_at": "x"}]))
        self.assertEqual(dash.values[0], ["Metric", "Value"])


class WindowTests(unittest.TestCase):
    def test_send_window(self):
        from zoneinfo import ZoneInfo
        import main
        c = cfg()
        ny = ZoneInfo("America/New_York")
        self.assertTrue(main.in_send_window(c, datetime(2026, 10, 6, 10, 0, tzinfo=ny)))   # Tue 10am
        self.assertFalse(main.in_send_window(c, datetime(2026, 10, 6, 20, 0, tzinfo=ny)))  # Tue 8pm
        self.assertFalse(main.in_send_window(c, datetime(2026, 10, 3, 10, 0, tzinfo=ny)))  # Sat


if __name__ == "__main__":
    unittest.main()
