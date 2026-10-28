import re

from fastapi.testclient import TestClient

from app.main import create_app
from app.payouts import FakeAirtimeClient
from app.sms.at_client import FakeSmsClient
from app.translators.base import FakeTranslator
from tests.conftest import Clock, make_settings

ADMIN = {"Authorization": "Bearer t"}
SP = "+254711000011"


def make(table):
    sms, clock = FakeSmsClient(), Clock()
    tr = FakeTranslator(table)
    app = create_app(make_settings(), sms_client=sms, clock=clock, start_scheduler=False,
                     translator=tr, airtime_client=FakeAirtimeClient())
    c = TestClient(app)
    c.post("/api/speakers", headers=ADMIN, json={"phone": SP, "languages": ["swh", "dav"]})
    c.post("/at/sms/inbound", data={"from": SP, "text": "YES", "id": "y"})
    c.post("/api/lists/L/recipients", headers=ADMIN, content="0711000001,dav\n")
    return app, c, sms, clock, tr


def campaign(c, **src):
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "L", "sources": [src]}).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    return cid


def states(c, cid):
    return {i["lang"]: i["state"] for i in c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()["items"]}


def test_customer_swahili_gets_space_draft_before_tasks():
    app, c, sms, clock, tr = make({("Nawa mikono", "dav"): "Choka yacha"})
    cid = campaign(c, text="Wash hands", swahili_text="Nawa mikono")
    assert states(c, cid) == {"dav": "drafting"}
    n_before = len(sms.to(SP))
    assert app.state.draft() == 1
    assert tr.calls == [("Nawa mikono", "swh", "dav")]
    assert states(c, cid) == {"dav": "fanout"}
    msgs = sms.to(SP)[n_before:]
    assert any('Machine: "Choka yacha"' in m and 'SW: "Nawa mikono"' in m for m in msgs)


def test_translator_gives_nothing_speakers_translate_from_scratch():
    app, c, sms, clock, tr = make({})
    cid = campaign(c, text="Wash hands", swahili_text="Nawa mikono")
    app.state.draft()
    assert states(c, cid) == {"dav": "fanout"}
    assert "Translate into Kidaw'ida" in sms.to(SP)[-1]


def test_english_only_no_supported_pair_starts_without_draft():
    app, c, sms, clock, tr = make({})
    cid = campaign(c, text="Wash hands")
    st = states(c, cid)
    assert st["dav"] == "fanout" and st["swh"] == "fanout" and tr.calls == []
    # the only speaker doesn't read English, so nobody can be tasked yet
    assert "You're in" in sms.to(SP)[-1]


def test_swahili_from_scratch_then_dav_drafted_from_it():
    app, c, sms, clock, tr = make({("Nawa mikono", "dav"): "Choka yacha"})
    other = "+254711000012"
    c.post("/api/speakers", headers=ADMIN, json={"phone": other, "languages": ["eng", "swh"]})
    c.post("/at/sms/inbound", data={"from": other, "text": "YES", "id": "y2"})
    cid = campaign(c, text="Wash hands")
    clock.advance(minutes=2)
    code = re.search(r"([A-Z2-9]{4}) Translate into Kiswahili",
                     " ".join(sms.to(other))).group(1)
    c.post("/at/sms/inbound", data={"from": other, "text": f"{code} Nawa mikono", "id": "r"})
    assert app.state.retranslate() == 1
    assert 'Machine: "Choka yacha"' in sms.to(SP)[-1]


def test_uploaded_candidate_never_drafted():
    app, c, sms, clock, tr = make({("Nawa mikono", "dav"): "X"})
    cid = c.post("/api/campaigns", headers=ADMIN, json={"sources": [
        {"text": "Wash hands", "swahili_text": "Nawa mikono",
         "candidates": {"dav": {"text": "Mine", "origin": "uploaded"}}}]}).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    assert states(c, cid) == {"dav": "fanout"} and tr.calls == []


def test_draft_after_deadline_closes_no_reply():
    app, c, sms, clock, tr = make({("Nawa mikono", "dav"): "Choka yacha"})
    cid = campaign(c, text="Wash hands", swahili_text="Nawa mikono")
    clock.advance(minutes=200)
    app.state.draft()
    items = c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()["items"]
    assert items[0]["outcome"] == "no_reply"
    app.state.tick()
    assert c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()["status"] == "delivered"
    assert sms.to("+254711000001")[-1] == "Wash hands"
