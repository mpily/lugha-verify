"""Shared shortcode: replies start with AT_KEYWORD (e.g. "LUGHA K7P2 5")."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.main import create_app
from app.models import Payout, Recipient, Speaker
from app.payouts import FakeAirtimeClient
from app.sms import templates as T
from app.sms.at_client import FakeSmsClient
from app.sms.parser import Keyword, ParseError, Reply, parse_sms
from app.sms.segments import count_segments
from tests.conftest import Clock, World, make_settings

ADMIN = {"Authorization": "Bearer t"}
PHONE = "+254711000009"


@pytest.mark.parametrize("raw,expected", [
    ("LUGHA K7P2 5", Reply("K7P2", 5, None, False)),
    ("lugha k7p2 3 Choka iyacha SHARE", Reply("K7P2", 3, "Choka iyacha", True)),
    ("Lugha: K7P2 Choka yacha", Reply("K7P2", None, "Choka yacha", False)),
    ("LUGHA YES", Keyword("YES")), ("lugha stop", Keyword("STOP")),
    ("LUGHA HELP", Keyword("HELP")),
    ("K7P2 5", Reply("K7P2", 5, None, False)),  # keyword forgotten: still read
    ("YES", Keyword("YES")),
    ("LUGHA", ParseError("empty")),
    ("LUGHA hello", ParseError("bad_code")),
])
def test_parse_with_keyword(raw, expected):
    assert parse_sms(raw, "LUGHA") == expected


def test_no_keyword_setting_leaves_parsing_unchanged():
    assert parse_sms("LUGHA K7P2 5") == ParseError("bad_code")  # no keyword configured


def test_texts_show_keyword():
    t = T.Templates("lugha")
    assert "Reply: LUGHA K7P2 5  or  LUGHA K7P2 3 <your version>" in \
        t.task_sms("K7P2", "dav", "Choka yacha", "Wash your hands")
    assert "Reply: LUGHA K7P2 <your translation>" in t.task_sms("K7P2", "dav", None, "x")
    assert "Reply LUGHA YES to join, LUGHA STOP to leave" in t.opt_in_prompt()
    assert "LUGHA <CODE> <1-5>" in t.help_sms()
    assert "LUGHA K7P2 <stars>" in t.no_correction_sms("K7P2")
    assert "Reply LUGHA YES" in t.not_opted_in_sms()
    assert "Reply LUGHA YES" in t.recipient_stop_sms()
    # No keyword: texts as before.
    assert T.Templates().opt_in_prompt() == T.opt_in_prompt()
    assert "Reply YES to join, STOP to leave" in T.opt_in_prompt()


def test_texts_with_keyword_stay_gsm7():
    t = T.Templates("LUGHA")
    for text in (t.opt_in_prompt(), t.help_sms(), t.parse_error_sms(),
                 t.task_sms("K7P2", "dav", "Choka yacha", "Wash your hands before eating")):
        assert count_segments(text)[0] == "GSM-7"
    assert count_segments(t.opt_in_prompt())[1] <= 2


@pytest.mark.parametrize("bad", ["two words", "LUGHA!", "x" * 21, "ŁUGHA"])
def test_keyword_setting_validated(bad):
    with pytest.raises(ValidationError):
        make_settings(at_keyword=bad)


def test_keyword_setting_normalised():
    assert make_settings(at_keyword=" lugha ").at_keyword == "LUGHA"
    assert make_settings(at_keyword="").at_keyword is None


def test_review_flow_with_keyword():
    w = World(at_keyword="LUGHA")
    tr = w.speaker(trusted=["dav"])
    it = w.item(); w.engine.start_item(it)
    code = w.code_for(it, tr)
    assert f"Reply: LUGHA {code} 5" in w.outbox.to(tr.id)[0]
    w.clock.advance(minutes=2)
    w.reply(tr, f"LUGHA {code} 3 Choka iyacha")
    assert it.state == "closed" and it.outcome == "verified"
    assert {(p.speaker_id, p.amount_kes) for p in w.db.scalars(select(Payout))} == {(tr.id, 100)}


def test_api_opt_in_and_recipient_stop_with_keyword():
    s = make_settings(at_keyword="LUGHA")
    sms = FakeSmsClient()
    app = create_app(s, sms_client=sms, clock=Clock(), start_scheduler=False,
                     airtime_client=FakeAirtimeClient())
    c = TestClient(app)
    c.post("/api/speakers", headers=ADMIN, json={"phone": PHONE, "languages": ["dav"]})
    assert "Reply LUGHA YES" in sms.to(PHONE)[0]
    c.post("/at/sms/inbound", data={"from": PHONE, "text": "LUGHA YES", "id": "k1"})
    with app.state.session() as db:
        assert db.scalar(select(Speaker)).opted_in_at is not None

    rcp = "+254722000009"
    c.post("/api/lists/l1/recipients", headers=ADMIN, content=f"{rcp},dav")
    c.post("/at/sms/inbound", data={"from": rcp, "text": "lugha stop", "id": "k2"})
    with app.state.session() as db:
        assert db.scalar(select(Recipient)).opted_out_at is not None
    assert "Reply LUGHA YES to rejoin" in sms.to(rcp)[-1]
