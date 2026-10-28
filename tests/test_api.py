from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import InboundSms, OutboundSms, Speaker
from app.sms.at_client import FakeSmsClient, parse_send_response
from tests.conftest import Clock, make_settings

ADMIN = {"Authorization": "Bearer t"}
PHONE = "+254711000009"


def make(**kw):
    s = make_settings(**kw)
    sms = FakeSmsClient()
    clock = Clock()
    app = create_app(s, sms_client=sms, clock=clock, start_scheduler=False)
    return app, TestClient(app), sms, clock


def test_admin_required():
    app, c, *_ = make()
    assert c.post("/api/speakers", json={"phone": PHONE, "languages": ["dav"]}).status_code == 401
    assert c.post("/api/speakers", headers={"Authorization": "Bearer x"},
                  json={"phone": PHONE, "languages": ["dav"]}).status_code == 401


def test_speaker_gets_opt_in_and_yes_works():
    app, c, sms, _ = make()
    r = c.post("/api/speakers", headers=ADMIN, json={"phone": "0711000009",
                                                    "languages": ["eng", "dav"]})
    assert r.status_code == 200 and "phone" not in r.json()
    assert "Reply YES" in sms.to(PHONE)[0]
    c.post("/at/sms/inbound", data={"from": PHONE, "text": "yes", "id": "a1"})
    assert r.json()["opted_in"] is False
    with app.state.session() as db:
        assert db.scalar(select(Speaker)).opted_in_at is not None


def test_callback_token_enforced():
    app, c, *_ = make(at_callback_token="sekret")
    d = {"from": PHONE, "text": "YES", "id": "x"}
    assert c.post("/at/sms/inbound", data=d).status_code == 403
    assert c.post("/at/sms/inbound?token=nope", data=d).status_code == 403
    assert c.post("/at/sms/inbound?token=sekret", data=d).status_code == 200


def test_inbound_retry_handled_once():
    app, c, sms, _ = make()
    c.post("/api/speakers", headers=ADMIN, json={"phone": PHONE, "languages": ["dav"]})
    for _ in range(3):
        c.post("/at/sms/inbound", data={"from": PHONE, "text": "HELP", "id": "dup"})
    assert sum("Reply <CODE>" in m for m in sms.to(PHONE)) == 1
    with app.state.session() as db:
        assert len(db.scalars(select(InboundSms)).all()) == 1


def test_delivery_report_updates_row():
    app, c, sms, _ = make()
    c.post("/api/speakers", headers=ADMIN, json={"phone": PHONE, "languages": ["dav"]})
    c.post("/at/sms/delivery", data={"id": "ATPid_fake_1", "status": "Failed",
                                     "failureReason": "AbsentSubscriber",
                                     "phoneNumber": PHONE})
    with app.state.session() as db:
        row = db.scalar(select(OutboundSms))
        assert row.delivery_status == "Failed" and row.failure_reason == "AbsentSubscriber"


def test_send_retried_after_network_error():
    app, c, sms, _ = make()
    sms.fail_next = 1
    c.post("/api/speakers", headers=ADMIN, json={"phone": PHONE, "languages": ["dav"]})
    with app.state.session() as db:
        row = db.scalar(select(OutboundSms))
        assert row.status == "queued" and row.attempts == 1
    app.state.flush()
    with app.state.session() as db:
        assert db.scalar(select(OutboundSms)).status == "sent"
    assert len(sms.to(PHONE)) == 1


def test_unknown_number_not_answered():
    app, c, sms, _ = make()
    c.post("/at/sms/inbound", data={"from": PHONE, "text": "K7P2 5", "id": "u"})
    assert sms.sent == []


def test_campaign_start_twice_conflicts():
    app, c, *_ = make()
    r = c.post("/api/campaigns", headers=ADMIN, json={"sources": [
        {"text": "Hello", "candidates": {"dav": {"text": "X"}}}]})
    cid = r.json()["id"]
    assert c.post(f"/api/campaigns/{cid}/start", headers=ADMIN).status_code == 200
    assert c.post(f"/api/campaigns/{cid}/start", headers=ADMIN).status_code == 409
    assert c.post("/api/campaigns/999/start", headers=ADMIN).status_code == 404


def test_parse_at_send_response():
    ok = parse_send_response({"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
        {"statusCode": 101, "number": PHONE, "status": "Success",
         "cost": "KES 0.8000", "messageId": "ATXid_1"}]}})
    assert ok.ok and ok.message_id == "ATXid_1"
    bad = parse_send_response({"SMSMessageData": {"Message": "x", "Recipients": [
        {"statusCode": 403, "number": PHONE, "status": "InvalidPhoneNumber",
         "cost": "0", "messageId": "None"}]}})
    assert not bad.ok and bad.status == "InvalidPhoneNumber" and bad.message_id is None
    assert not parse_send_response({"SMSMessageData": {"Message": "InvalidSenderId",
                                                       "Recipients": []}}).ok


def test_simulation_offline():
    from scripts.simulate import main
    assert main([]) == 0
