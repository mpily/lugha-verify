"""End-to-end run with simulated speakers (CLAUDE.md §12).

Scenario (user's example, 2026-10-28): an English-only notice; reviewers
  B, C: Kiswahili + Kidaw'ida     D: English + Kiswahili + Kidaw'ida
  E: English + Kiswahili (Swahili reviewer)
D gets the Kidaw'ida task (English source), E the machine Swahili. After E
corrects the Swahili, B and C get the Kidaw'ida task with E's Swahili.

Speaker replies are posted to /at/sms/inbound in AT's callback format. The
clock is simulated, so the 30 min / 2 h timers run instantly.

  python scripts/simulate.py           # offline: SMS recorded, not sent
  python scripts/simulate.py --live    # outbound SMS via the AT sandbox;
      needs AT_USERNAME=sandbox, AT_API_KEY, AT_SHORTCODE in the environment.
      Phones are AT sandbox simulator numbers; open the simulator to watch.
Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Delivery, OutboundSms, Payout, ReviewTask, Translation  # noqa: E402
from app.payouts import FakeAirtimeClient  # noqa: E402
from app.sms.at_client import FakeSmsClient  # noqa: E402
from app.translators.base import FakeTranslator  # noqa: E402

PHONES = {"B": "+254711000001", "C": "+254711000002",
          "D": "+254711000003", "E": "+254711000004"}
RECIPIENTS = {"+254722000001": "eng", "+254722000002": "eng", "+254722000003": "swh",
              "+254722000004": "dav", "+254722000005": "dav", "+254722000006": "dav"}
LANGS = {"B": ["swh", "dav"], "C": ["swh", "dav"],
         "D": ["eng", "swh", "dav"], "E": ["eng", "swh"]}
EN = "Wash your hands with soap before eating food"
SW = "Nawa mikono kwa sabuni kabla ya kula chakula"
SW_FIXED = "Osha mikono kwa sabuni kabla ya kula chakula"
DAV = "Choka yacha"  # placeholder candidates, not real Kidaw'ida for EN
DAV2 = "Choka iyacha"  # what the (fake) translator makes from E's Swahili


class SimClock:
    def __init__(self):
        self.now = datetime(2026, 10, 29, 9, 0)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="send SMS via AT sandbox")
    args = ap.parse_args(argv)

    clock = SimClock()
    if args.live:
        settings = Settings(database_url="sqlite://", at_callback_token=None)
        if not settings.at_sandbox:
            print("refusing --live outside the sandbox (AT_USERNAME must be 'sandbox')")
            return 2
        client = air = None  # real AT clients
    else:
        settings = Settings(_env_file=None, at_username="sandbox", at_api_key="x",
                            at_shortcode="12345", phone_enc_key=Fernet.generate_key().decode(),
                            phone_hash_salt="sim", admin_token="sim",
                            database_url="sqlite://")
        client, air = FakeSmsClient(), FakeAirtimeClient()
    # The translator is always simulated: the Space is a separate service.
    translator = FakeTranslator({(SW_FIXED, "dav"): DAV2})
    app = create_app(settings, sms_client=client, clock=clock, start_scheduler=False,
                     translator=translator, airtime_client=air)
    admin = {"Authorization": "Bearer " + settings.admin_token.get_secret_value()}
    tok = settings.at_callback_token
    q = f"?token={tok.get_secret_value()}" if tok else ""
    failures: list[str] = []
    msg_id = iter(range(1, 10_000))
    ids: dict[str, int] = {}

    def check(cond, what):
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            failures.append(what)

    def last_sms(who):
        with app.state.session() as db:
            row = db.scalars(select(OutboundSms).where(OutboundSms.speaker_id == ids[who])
                             .order_by(OutboundSms.id.desc())).first()
            return row.text if row else ""

    def sms(who, text):
        c.post(f"/at/sms/inbound{q}", data={
            "from": PHONES[who], "to": settings.at_sender, "text": text,
            "id": f"sim-{next(msg_id)}", "date": clock.now.isoformat()})

    def code(who):
        m = re.match(r"([A-Z2-9]{4}) ", last_sms(who))
        return m.group(1) if m else "????"

    def task_of(who, lang):
        with app.state.session() as db:
            for t in db.scalars(select(ReviewTask).where(ReviewTask.speaker_id == ids[who])):
                if t.item.lang == lang:
                    return t
        return None

    with TestClient(app) as c:
        print("1. register speakers, opt in")
        for who in PHONES:
            r = c.post("/api/speakers", headers=admin,
                       json={"phone": PHONES[who], "languages": LANGS[who]})
            ids[who] = r.json()["id"]
            check("Reply YES" in last_sms(who), f"{who} got the opt-in prompt")
            sms(who, "YES")
            check(r.status_code == 200 and "You're in" in last_sms(who), f"{who} opted in")

        print("2. recipient list + English-only campaign (machine Swahili, Kidaw'ida)")
        csv_ = "phone,preferred_lang\n" + "\n".join(f"{p},{l}" for p, l in RECIPIENTS.items())
        r = c.post("/api/lists/clinic/recipients", headers=admin, content=csv_)
        check(r.json()["added"] == 6 and r.json()["invalid_lines"] == [], "6 recipients uploaded")
        c.post(f"/at/sms/inbound{q}", data={"from": "+254722000006", "text": "STOP", "id": "r1"})
        r = c.post("/api/campaigns", headers=admin, json={
            "mode": "bulk", "source_lang": "eng", "fallback_lang": "eng", "list_id": "clinic",
            "sources": [{"text": EN, "candidates": {
                "swh": {"text": SW, "origin": "default_api", "model_ref": "nllb"},
                "dav": {"text": DAV, "origin": "default_api", "model_ref": "space"}}}]})
        cid = r.json()["id"]
        check(r.json()["review_items"] == 2, "2 review items (swh, dav)")
        c.post(f"/api/campaigns/{cid}/start", headers=admin)
        check(task_of("D", "dav") is not None and task_of("D", "dav").shown_source_lang == "eng",
              "D got the Kidaw'ida task with the English source")
        check(task_of("E", "swh") is not None, "E got the machine Swahili task")
        check(task_of("B", "dav") is None and task_of("C", "dav") is None,
              "B, C not tasked yet (no Swahili source)")

        print("3. E corrects the Swahili -> Kidaw'ida re-translated -> B and C get it")
        clock.advance(minutes=3)
        sms("E", f"{code('E')} 3 {SW_FIXED} SHARE")
        check(task_of("B", "dav") is None, "B waits for the re-translation")
        check(app.state.retranslate() == 1 and translator.calls == [(SW_FIXED, "swh", "dav")],
              "re-translated from E's Swahili")
        for who in ("B", "C"):
            t = task_of(who, "dav")
            check(t is not None and t.shown_source_text == SW_FIXED
                  and t.shown_candidate_text == DAV2,
                  f"{who} got the new Kidaw'ida draft with E's Swahili")

        print("4. D corrects to the new draft, B accepts it -> verified, paid 50 each")
        clock.advance(minutes=4)
        sms("D", f"{code('D')} 3 {DAV2}")
        sms("B", f"{code('B')} 5")
        check("is filled" in last_sms("C"), "C told the task is filled")
        c_code = task_of("C", "dav").code
        sms("C", f"{c_code} 5")
        check(task_of("C", "dav").status == "late", "C's late reply stored, unpaid")

        print("5. deadline passes; Swahili had one reply -> not verified")
        clock.advance(minutes=130)
        app.state.tick()
        sms("E", "STOP")
        check("left Lugha Verify" in last_sms("E"), "E opted out")

        st = c.get(f"/api/campaigns/{cid}", headers=admin).json()
        out = {i["lang"]: i["outcome"] for i in st["items"]}
        check(out == {"dav": "verified", "swh": "unverified"}, f"outcomes {out}")
        with app.state.session() as db:
            pays = sorted((p.speaker_id, p.amount_kes) for p in db.scalars(select(Payout)))
            want = sorted([(ids["D"], 50), (ids["B"], 50), (ids["E"], 50)])
            check(pays == want, f"payouts {pays}")
            st_ = {p.status for p in db.scalars(select(Payout))}
            check(st_ == {"sent"}, f"airtime sent for every payout ({st_})")
            trs = {t.lang: t.status for t in db.scalars(select(Translation))}
            check(trs == {"dav": "verified", "swh": "speaker_corrected"}, f"translations {trs}")
            rows = db.scalars(select(OutboundSms)).all()
            sent = sum(r.status == "sent" for r in rows)
            check(sent == len(rows), f"{sent}/{len(rows)} SMS sent "
                                     f"({sum(r.segments or 0 for r in rows)} segments)")
            raw = " ".join(str(v) for r in rows for v in (r.phone_enc, r.text))
            check(not any(p in raw for p in [*PHONES.values(), *RECIPIENTS]),
                  "no plain phone numbers stored")

        print("6. delivery: verified Kidaw'ida; unverified Swahili falls back to English")
        check(st["status"] == "delivered", f"campaign {st['status']}")
        check(st["deliveries"] == {"eng": {"n": 3, "fallback": 1, "delivered": 0},
                                   "dav": {"n": 2, "fallback": 0, "delivered": 0}},
              f"deliveries {st['deliveries']}")
        with app.state.session() as db:
            got = {d.recipient_id: (d.lang_sent, d.text_sent, d.was_fallback)
                   for d in db.scalars(select(Delivery))}
        texts = {t for _, t, _ in got.values()}
        check(texts == {EN, DAV2}, f"texts sent {texts}")
        check(SW not in texts and SW_FIXED not in texts and DAV not in texts,
              "no unverified text delivered")
        check(len(got) == 5, "opted-out recipient skipped")
        app.state.tick()
        with app.state.session() as db:
            check(len(db.scalars(select(Delivery)).all()) == 5, "second tick sends nothing")

        print("7. research report")
        rep = c.get(f"/api/campaigns/{cid}/report", headers=admin).json()
        rows = {(r["lang"], r["model_ref"]): r for r in rep["rows"]}
        d1 = rows.get(("dav", "space"))
        d2 = rows.get(("dav", "fake"))
        check(d1 is not None and d1["n_replies"] == 1 and d1["stars"]["mean"] == 3.0,
              "D's rating of the first draft reported")
        check(d2 is not None and d2["n_replies"] == 2 and d2["low_n"],
              "B and C's ratings of the re-translated draft reported, flagged low n")
        check(("swh", "nllb") in rows and rows[("swh", "nllb")]["chrf"]["n"] == 1,
              "Swahili draft scored (chrF vs E's correction)")
        for r in rep["rows"]:
            print(f"       {r['lang']} {r['model_ref']}: items={r['n_items']} "
                  f"stars={r['stars']['mean']} chrF={r['chrf']['mean'] and round(r['chrf']['mean'], 1)}")

    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
