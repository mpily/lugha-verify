"""Optional second AT account (AT_DELIVERY_*) used only for the final message
to recipients; tasks, replies and opt-ins stay on the main account."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.payouts import FakeAirtimeClient
from app.sms.at_client import FakeSmsClient
from tests.conftest import Clock, make_settings

ADMIN = {"Authorization": "Bearer t"}
SPEAKER = "+254711000009"
RECIPIENT = "+254722000009"

LIVE = dict(at_delivery_username="BillMbogho", at_delivery_api_key="live",
            at_delivery_sender="AFTKNG")


@pytest.mark.parametrize("partial", [
    {"at_delivery_username": "x"},
    {"at_delivery_username": "x", "at_delivery_api_key": "k"},
    {"at_delivery_sender": "AFTKNG"},
])
def test_delivery_settings_all_or_none(partial):
    with pytest.raises(ValidationError):
        make_settings(**partial)


def test_delivery_sandbox_flag():
    assert make_settings().delivery_sandbox is True           # main account = sandbox
    assert make_settings(**LIVE).delivery_sandbox is False    # live delivery account
    assert make_settings(**LIVE).at_sandbox is True           # tasks still on sandbox


def _campaign_to_delivery(c, clock):
    c.post("/api/speakers", headers=ADMIN, json={"phone": SPEAKER, "languages": ["eng", "dav"],
                                                 "trusted_for": ["dav"]})
    c.post("/at/sms/inbound", data={"from": SPEAKER, "text": "YES", "id": "y1"})
    c.post("/api/lists/l1/recipients", headers=ADMIN, content=f"{RECIPIENT},dav")
    cid = c.post("/api/campaigns", headers=ADMIN, json={
        "mode": "bulk", "list_id": "l1", "review_deadline_min": 30,
        "sources": [{"text": "Wash your hands", "candidates": {"dav": {"text": "Choka yacha"}}}],
    }).json()["id"]
    c.post(f"/api/campaigns/{cid}/start", headers=ADMIN)
    return cid


def test_only_recipient_delivery_uses_delivery_account():
    main, live = FakeSmsClient(), FakeSmsClient()
    clock = Clock()
    app = create_app(make_settings(**LIVE), sms_client=main, delivery_sms_client=live,
                     clock=clock, start_scheduler=False, airtime_client=FakeAirtimeClient())
    c = TestClient(app)
    cid = _campaign_to_delivery(c, clock)
    task = main.to(SPEAKER)[-1]
    code = task.split()[0]
    clock.advance(minutes=1)
    c.post("/at/sms/inbound", data={"from": SPEAKER, "text": f"{code} 5", "id": "r1"})
    clock.advance(minutes=31)
    app.state.tick()

    assert live.to(RECIPIENT) == ["Choka yacha"]      # final message: live account
    assert main.to(RECIPIENT) == []
    assert live.to(SPEAKER) == []                     # opt-in, task, thanks: main account
    assert len(main.to(SPEAKER)) >= 3
    st = c.get(f"/api/campaigns/{cid}", headers=ADMIN).json()
    assert st["status"] == "delivered"


def test_without_delivery_account_everything_uses_main():
    main = FakeSmsClient()
    clock = Clock()
    app = create_app(make_settings(), sms_client=main, clock=clock, start_scheduler=False,
                     airtime_client=FakeAirtimeClient())
    c = TestClient(app)
    _campaign_to_delivery(c, clock)
    code = main.to(SPEAKER)[-1].split()[0]
    clock.advance(minutes=1)
    c.post("/at/sms/inbound", data={"from": SPEAKER, "text": f"{code} 5", "id": "r1"})
    clock.advance(minutes=31)
    app.state.tick()
    assert main.to(RECIPIENT) == ["Choka yacha"]


def test_health_reports_both_accounts():
    app = create_app(make_settings(**LIVE), sms_client=FakeSmsClient(),
                     delivery_sms_client=FakeSmsClient(), clock=Clock(),
                     start_scheduler=False, airtime_client=FakeAirtimeClient())
    assert TestClient(app).get("/health").json() == {
        "ok": True, "at_sandbox": True, "delivery_sandbox": False}
