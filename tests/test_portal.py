import re

from fastapi.testclient import TestClient

from app.main import create_app
from app.payouts import FakeAirtimeClient
from app.sms.at_client import FakeSmsClient
from app.translators.base import FakeTranslator
from tests.conftest import Clock, make_settings


class Sink:
    def __init__(self):
        self.records = []

    def write(self, records):
        self.records += records


def make():
    sms, clock, sink = FakeSmsClient(), Clock(), Sink()
    app = create_app(make_settings(), sms_client=sms, clock=clock, start_scheduler=False,
                     translator=FakeTranslator(), airtime_client=FakeAirtimeClient(),
                     shared_sink=sink)
    return app, TestClient(app), sms, clock, sink


def login(c):
    r = c.post("/portal/login", data={"token": "t"}, follow_redirects=False)
    assert r.status_code == 303
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/portal/lists").text).group(1)


def test_requires_login():
    app, c, *_ = make()
    for path in ("/portal", "/portal/lists", "/portal/campaigns/new",
                 "/portal/campaigns/1/export.csv"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/portal/login"
    assert c.post("/portal/login", data={"token": "wrong"}).status_code == 401


def test_cookie_is_not_the_token():
    app, c, *_ = make()
    r = c.post("/portal/login", data={"token": "t"}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "lv_session=t;" not in cookie


def test_csrf_required():
    app, c, *_ = make()
    login(c)
    r = c.post("/portal/speakers", data={"phone": "0711000001", "languages": "dav"})
    assert r.status_code == 403


def test_full_bulk_flow_through_portal():
    app, c, sms, clock, sink = make()
    csrf = login(c)
    r = c.post("/portal/lists", data={"csrf": csrf, "list_id": "clinic",
                                      "csv_text": "phone,preferred_lang\n0711000001,eng\n"
                                                  "0711000002,dav\nnope,dav\n"})
    assert "2 added" in r.text and "Invalid lines: 4" in r.text
    for phone, langs in (("0711000011", "eng, dav"), ("0711000012", "eng,dav")):
        r = c.post("/portal/speakers", data={"csrf": csrf, "phone": phone, "languages": langs})
        assert "opt-in SMS sent" in r.text
        c.post("/at/sms/inbound", data={"from": phone, "text": "YES", "id": phone})
    r = c.post("/portal/campaigns", data={
        "csrf": csrf, "mode": "bulk", "list_id": "clinic", "source_lang": "eng",
        "fallback_lang": "eng", "message": "Clinic opens at 9. Bring your card.",
        "review_deadline_min": "60"}, follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    page = c.get(url).text
    assert "Clinic opens at 9." in page and "Bring your card." in page  # split
    assert "Start review" in page
    c.post(url + "/start", data={"csrf": csrf})
    assert "Start review" not in c.get(url).text
    # both speakers translate both dav sentences identically
    clock.advance(minutes=2)
    for phone in ("+254711000011", "+254711000012"):
        for m in sms.to(phone):
            code = re.match(r"([A-Z2-9]{4}) Translate into Kidaw'ida", m)
            if code:
                txt = "Kiliniki" if "Clinic" in m else "Kadi"
                c.post("/at/sms/inbound", data={"from": phone, "id": f"{phone}{code[1]}",
                                                "text": f"{code[1]} {txt} SHARE"})
    clock.advance(minutes=61); app.state.tick()
    page = c.get(url).text
    assert "delivered" in page and "verified" in page
    assert sms.to("+254711000002")[-1] == "Kiliniki Kadi"
    assert sms.to("+254711000001")[-1] == "Clinic opens at 9. Bring your card."
    csv_ = c.get(url + "/export.csv").text
    assert csv_.splitlines()[0].startswith("campaign_id,source_id")
    assert "Kiliniki" in csv_ and "0711000011" not in csv_ and "254711" not in csv_
    # consent export: 4 SHARE replies, anonymous
    assert app.state.share() == 4 and app.state.share() == 0
    assert all("speaker_id" not in r and "campaign_id" not in r for r in sink.records)
    assert {r["corrected_text"] for r in sink.records} == {"Kiliniki", "Kadi"}


def test_research_csv_and_errors():
    app, c, *_ = make()
    csrf = login(c)
    r = c.post("/portal/campaigns", data={
        "csrf": csrf, "mode": "research", "source_lang": "eng", "model_ref": "my-mt",
        "csv_text": "source,lang,candidate\nHello,dav,Kuhoo\nHello,luo,\nBye,dav,Kwaheri\n"},
        follow_redirects=False)
    assert r.status_code == 303
    page = c.get(r.headers["location"]).text
    assert page.count("<td>Hello</td>") == 2 and "no draft" in page
    r = c.post("/portal/campaigns", data={"csrf": csrf, "mode": "research",
                                          "csv_text": "only-one-column\n"})
    assert r.status_code == 422 and "need source and lang" in r.text
    r = c.post("/portal/campaigns", data={"csrf": csrf, "mode": "bulk", "list_id": "none",
                                          "message": "Hi"})
    assert r.status_code == 422 and "recipient list" in r.text


def test_pages_are_small():
    app, c, *_ = make()
    login(c)
    for p in ("/portal", "/portal/lists", "/portal/speakers", "/portal/campaigns/new"):
        body = c.get(p).content
        assert len(body) < 8000 and b"<script" not in body, p


def test_rest_export():
    app, c, *_ = make()
    h = {"Authorization": "Bearer t"}
    cid = c.post("/api/campaigns", headers=h, json={"sources": [
        {"text": "Hi", "candidates": {"dav": {"text": "X"}}}]}).json()["id"]
    assert c.get(f"/api/campaigns/{cid}/export?format=csv", headers=h).status_code == 200
    assert c.get("/api/campaigns/99/export", headers=h).status_code == 404
    assert c.get(f"/api/campaigns/{cid}/export").status_code == 401
