"""AT's sandbox sometimes answers a valid key with 401. A 401 means AT did
nothing, so the SMS and airtime clients try the same request again (only for
401; other errors keep their existing handling)."""
import json

import httpx

from app.payouts import ATAirtimeClient
from app.sms.at_client import ATClient

SMS_OK = {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
    {"statusCode": 101, "status": "Success", "messageId": "ATXid_1", "cost": "KES 0.8000",
     "number": "+254711000001"}]}}
AIR_OK = {"errorMessage": "None", "responses": [
    {"status": "Sent", "requestId": "ATQid_1", "errorMessage": "None"}]}


def scripted(statuses, ok_body):
    """Transport answering with the given status codes in order."""
    calls = []

    def handler(req):
        calls.append(req)
        code = statuses[min(len(calls), len(statuses)) - 1]
        if code == 401:
            return httpx.Response(401, text="The supplied authentication is invalid")
        return httpx.Response(code, json=ok_body)
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def sms_client(statuses, **kw):
    c = ATClient("sandbox", "k", "58008", sandbox=True, retry_delay=0, **kw)
    c.http, calls = scripted(statuses, SMS_OK)
    return c, calls


def air_client(statuses, **kw):
    c = ATAirtimeClient("sandbox", "k", sandbox=True, retry_delay=0, **kw)
    c.http, calls = scripted(statuses, AIR_OK)
    return c, calls


def test_sms_retries_401_then_succeeds():
    c, calls = sms_client([401, 401, 201])
    r = c.send("+254711000001", "hi")
    assert r.ok and r.message_id == "ATXid_1" and len(calls) == 3
    # the same request each time
    assert len({bytes(x.content) for x in calls}) == 1


def test_sms_gives_up_after_retries():
    c, calls = sms_client([401], auth_retries=4)
    r = c.send("+254711000001", "hi")
    assert not r.ok and r.status == "http_401" and len(calls) == 5


def test_sms_no_retry_on_other_errors_or_success():
    c, calls = sms_client([500])
    assert c.send("+254711000001", "hi").status == "http_500" and len(calls) == 1
    c, calls = sms_client([201])
    assert c.send("+254711000001", "hi").ok and len(calls) == 1


def test_airtime_retries_401_then_sent():
    c, calls = air_client([401, 201])
    r = c.send("+254711000001", 50)
    assert r.state == "sent" and r.request_id == "ATQid_1" and len(calls) == 2
    assert len({bytes(x.content) for x in calls}) == 1
    sent = dict(httpx.QueryParams(calls[0].content.decode()))
    assert json.loads(sent["recipients"])[0]["amount"] == "KES 50"


def test_airtime_401_exhausted_is_failed_not_unknown():
    c, calls = air_client([401], auth_retries=2)
    r = c.send("+254711000001", 50)
    assert r.state == "failed" and len(calls) == 3


def test_airtime_5xx_not_retried():
    c, calls = air_client([503])
    assert c.send("+254711000001", 50).state == "unknown" and len(calls) == 1
