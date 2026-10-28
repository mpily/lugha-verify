from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from app.delivery import SentenceInfo, compose_message
from app.main import create_app
from app.models import Delivery, Recipient
from app.payouts import FakeAirtimeClient
from app.sms.at_client import FakeSmsClient
from app.translators.base import FakeTranslator
from tests.conftest import Clock, make_settings

ADMIN = {"Authorization": "Bearer t"}
S = make_settings()
STATUSES = [None, "machine", "speaker_corrected", "verified"]

sentence = st.builds(
    SentenceInfo, source_text=st.sampled_from(["S1", "S2"]),
    customer_swahili=st.sampled_from([None, "CSW"]),
    verified_swahili=st.sampled_from([None, "VSW"]),
    translation_text=st.sampled_from([None, "T"]),
    translation_status=st.sampled_from(STATUSES))


@given(rl=st.sampled_from(["eng", "swh", "dav", "luo"]), sl=st.sampled_from(["eng", "swh"]),
       fl=st.sampled_from(["eng", "swh"]), sents=st.lists(sentence, min_size=1, max_size=3))
def test_message_never_contains_unverified_review_text(rl, sl, fl, sents):
    m = compose_message(recipient_lang=rl, source_lang=sl, fallback_lang=fl,
                        sentences=sents, needs_review_for=S.needs_review_for)
    parts = m.text.split(" ")
    assert len(parts) == len(sents)  # whole message, one language
    for p, s in zip(parts, sents):
        allowed = {s.source_text, s.customer_swahili, s.verified_swahili}
        if s.translation_status == "verified" or not S.needs_review_for(
                rl, bool(s.customer_swahili)):
            allowed.add(s.translation_text)
        assert p in allowed
    if m.was_fallback:
        assert "T" not in parts


def test_one_unverified_sentence_makes_whole_message_fallback():
    v = SentenceInfo("S1", None, "VSW1", "T1", "verified")
    u = SentenceInfo("S2", None, None, "T2", "machine")
    m = compose_message(recipient_lang="dav", source_lang="eng", fallback_lang="swh",
                        sentences=[v, u], needs_review_for=S.needs_review_for)
    assert (m.text, m.lang, m.was_fallback) == ("S1 S2", "eng", True)  # S2 has no Swahili
    u2 = SentenceInfo("S2", "CSW2", None, "T2", "machine")
    m = compose_message(recipient_lang="dav", source_lang="eng", fallback_lang="swh",
                        sentences=[v, u2], needs_review_for=S.needs_review_for)
    assert (m.text, m.lang) == ("VSW1 CSW2", "swh")


def make():
    sms = FakeSmsClient()
    clock = Clock()
    app = create_app(S, sms_client=sms, clock=clock, start_scheduler=False,
                     translator=FakeTranslator(), airtime_client=FakeAirtimeClient())
    return app, TestClient(app), sms, clock


def test_import_recipients_validation():
    app, c, *_ = make()
    r = c.post("/api/lists/L/recipients", headers=ADMIN,
               content="phone,preferred_lang\n0711000001,dav\nbad,dav\n0711000002\n"
                       "0711000003,d4v\n\n+254711000001,eng\n")
    assert r.json() == {"list_id": "L", "added": 1, "updated": 1, "invalid_lines": [3, 4, 5]}
    with app.state.session() as db:
        assert db.scalar(select(Recipient)).preferred_lang == "eng"


def test_bulk_without_review_delivers_at_start():
    app, c, sms, _ = make()
    c.post("/api/lists/L/recipients", headers=ADMIN, content="0711000001,eng\n0711000002,swh\n")
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "L", "sources": [{"text": "Hello", "swahili_text": "Habari"}]}
    ).json()["id"]
    r = c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    assert r.status_code == 200
    assert sms.to("+254711000001") == ["Hello"] and sms.to("+254711000002") == ["Habari"]
    assert c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()["status"] == "delivered"


def test_bulk_needs_list():
    app, c, *_ = make()
    r = c.post("/api/campaigns", headers=ADMIN, json={"mode": "bulk", "sources": [{"text": "x"}]})
    assert r.status_code == 422


def test_bulk_creates_items_for_list_languages_without_candidates():
    app, c, sms, clock = make()
    c.post("/api/lists/L/recipients", headers=ADMIN, content="0711000001,dav\n")
    r = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "L", "sources": [{"text": "Hello"}]})
    assert r.json()["review_items"] == 2  # dav + machine-Swahili pivot, no candidates
    cid = r.json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    clock.advance(minutes=121); app.state.tick()
    assert sms.to("+254711000001") == ["Hello"]  # nothing verified -> fallback


def test_research_campaign_finishes_without_delivery():
    app, c, sms, clock = make()
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "research", "sources": [{"text": "Hello", "candidates": {"dav": {"text": "X"}}}]}
    ).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    clock.advance(minutes=121); app.state.tick()
    st_ = c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()
    assert st_["status"] == "done" and st_["deliveries"] == {}


def test_recipient_stop_and_yes():
    app, c, sms, _ = make()
    c.post("/api/lists/L/recipients", headers=ADMIN, content="0711000001,eng\n")
    c.post("/at/sms/inbound", data={"from": "+254711000001", "text": "STOP", "id": "1"})
    assert "no more messages" in sms.to("+254711000001")[-1]
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "L", "sources": [{"text": "Hello"}]}).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    assert sms.to("+254711000001")[-1] != "Hello"
    c.post("/at/sms/inbound", data={"from": "+254711000001", "text": "YES", "id": "2"})
    with app.state.session() as db:
        assert db.scalar(select(Recipient)).opted_out_at is None


def test_delivery_report_reaches_delivery_row():
    app, c, sms, _ = make()
    c.post("/api/lists/L/recipients", headers=ADMIN, content="0711000001,eng\n")
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "L", "sources": [{"text": "Hello"}]}).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    with app.state.session() as db:
        d = db.scalar(select(Delivery))
        assert d.delivery_status == "sent" and d.at_message_id
        mid = d.at_message_id
    c.post("/at/sms/delivery", data={"id": mid, "status": "Success"})
    st_ = c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()
    assert st_["deliveries"] == {"eng": {"n": 1, "fallback": 0, "delivered": 1}}
