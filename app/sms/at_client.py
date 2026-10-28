"""Africa's Talking SMS client (form-encoded `version1/messaging` API).

Written from AT's documented API; developers.africastalking.com was not
reachable from the build sandbox, so check against a live sandbox call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

LIVE_URL = "https://api.africastalking.com/version1/messaging"
SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
OK_CODES = {100, 101, 102}  # Processed, Sent, Queued


@dataclass
class SendResult:
    ok: bool
    message_id: str | None
    status: str  # AT's status text, e.g. "Success", "InvalidPhoneNumber"
    cost: str | None = None


class SmsClient(Protocol):
    def send(self, phone: str, text: str) -> SendResult: ...


class ATClient:
    def __init__(self, username: str, api_key: str, sender: str | None,
                 sandbox: bool, timeout: float = 20.0):
        self.username, self.api_key, self.sender = username, api_key, sender
        self.url = SANDBOX_URL if sandbox else LIVE_URL
        self.http = httpx.Client(timeout=timeout)

    def send(self, phone: str, text: str) -> SendResult:
        data = {"username": self.username, "to": phone, "message": text}
        if self.sender:
            data["from"] = self.sender
        r = self.http.post(self.url, data=data, headers={
            "apiKey": self.api_key, "Accept": "application/json"})
        if r.status_code >= 400:
            return SendResult(False, None, f"http_{r.status_code}")
        return parse_send_response(r.json())


def parse_send_response(body: dict) -> SendResult:
    recips = (body.get("SMSMessageData") or {}).get("Recipients") or []
    if not recips:
        msg = (body.get("SMSMessageData") or {}).get("Message") or "no_recipients"
        return SendResult(False, None, str(msg)[:64])
    r = recips[0]
    code = int(r.get("statusCode", 0))
    mid = r.get("messageId")
    mid = None if mid in (None, "", "None") else str(mid)
    return SendResult(code in OK_CODES, mid,
                      str(r.get("status", code))[:64], r.get("cost"))


class FakeSmsClient:
    """Records messages instead of sending (tests, offline simulation)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_next = 0

    def send(self, phone: str, text: str) -> SendResult:
        if self.fail_next:
            self.fail_next -= 1
            raise httpx.ConnectError("simulated network failure")
        self.sent.append((phone, text))
        return SendResult(True, f"ATPid_fake_{len(self.sent)}", "Success", "KES 0.8000")

    def to(self, phone: str) -> list[str]:
        return [t for p, t in self.sent if p == phone]
