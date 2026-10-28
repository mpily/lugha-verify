from sqlalchemy import select

from app.models import Payout
from app.payouts import (AirtimeResult, FakeAirtimeClient, airtime_status_callback,
                         flush_payouts, parse_airtime_response)
from tests.conftest import KEY


def closed_item(w):
    ss = [w.speaker() for _ in range(2)]
    it = w.item(); w.engine.start_item(it)
    w.clock.advance(minutes=2)
    w.reply(ss[0], f"{w.code_for(it, ss[0])} 5"); w.reply(ss[1], f"{w.code_for(it, ss[1])} 5")
    w.db.commit()
    return ss


def rows(w):
    return list(w.db.scalars(select(Payout).order_by(Payout.id)))


def test_pays_each_task_once(w):
    ss = closed_item(w)
    air = FakeAirtimeClient()
    assert flush_payouts(w.db, air, KEY, w.clock) == 2
    assert flush_payouts(w.db, air, KEY, w.clock) == 0  # nothing left pending
    assert sorted(a for _, a in air.sent) == [50, 50]
    assert all(p.status == "sent" and p.at_transaction_id for p in rows(w))


def test_success_callback_idempotent(w):
    ss = closed_item(w)
    flush_payouts(w.db, FakeAirtimeClient(), KEY, w.clock)
    rid = rows(w)[0].at_transaction_id
    assert airtime_status_callback(w.db, rid, "Success", w.clock)
    assert not airtime_status_callback(w.db, rid, "Success", w.clock)  # duplicate
    assert not airtime_status_callback(w.db, rid, "Failed", w.clock)   # late, wrong
    assert ss[0].airtime_paid_total + ss[1].airtime_paid_total == 50
    assert rows(w)[0].status == "success"


def test_confirmed_failure_retried_then_gives_up(w):
    closed_item(w)
    air = FakeAirtimeClient()
    flush_payouts(w.db, air, KEY, w.clock)
    p = rows(w)[0]
    for i in range(3):
        rid = p.at_transaction_id
        assert airtime_status_callback(w.db, rid, "Failed", w.clock)
        if p.status == "pending":
            flush_payouts(w.db, air, KEY, w.clock)
    assert p.status == "failed" and p.attempts == 3


def test_connect_error_retried(w):
    closed_item(w)
    air = FakeAirtimeClient()
    air.next_results = [AirtimeResult("retry", error="ConnectError")]
    flush_payouts(w.db, air, KEY, w.clock)
    assert rows(w)[0].status == "pending"
    flush_payouts(w.db, air, KEY, w.clock)
    assert all(p.status == "sent" for p in rows(w)) and len(air.sent) == 2


def test_ambiguous_error_never_retried(w):
    closed_item(w)
    air = FakeAirtimeClient()
    air.next_results = [AirtimeResult("unknown", error="ReadTimeout")]
    flush_payouts(w.db, air, KEY, w.clock)
    flush_payouts(w.db, air, KEY, w.clock); flush_payouts(w.db, air, KEY, w.clock)
    assert rows(w)[0].status == "unknown" and len(air.sent) == 1


def test_client_crash_is_unknown(w):
    closed_item(w)

    class Boom:
        def send(self, phone, amount):
            raise RuntimeError("x")
    flush_payouts(w.db, Boom(), KEY, w.clock)
    assert {p.status for p in rows(w)} == {"unknown"}


def test_parse_response():
    ok = parse_airtime_response({"errorMessage": "None", "numSent": 1, "responses": [
        {"phoneNumber": "+254711000001", "errorMessage": "None", "amount": "KES 50.0000",
         "status": "Sent", "requestId": "ATQid_1", "discount": "KES 2.0000"}]})
    assert ok.state == "sent" and ok.request_id == "ATQid_1"
    bad = parse_airtime_response({"errorMessage": "None", "responses": [
        {"status": "Failed", "errorMessage": "Insufficient Credit", "requestId": "None"}]})
    assert bad.state == "failed" and bad.request_id is None and "Insufficient" in bad.error
    assert parse_airtime_response({"errorMessage": "Invalid", "responses": []}).state == "failed"
