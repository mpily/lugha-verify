"""Airtime payouts (CLAUDE.md §2 Money).

- One payout row per task (unique review_task_id): a task is never paid twice.
- Each row is committed as 'sending' BEFORE the AT call and updated with AT's
  requestId AFTER it.
- Retry only when we know AT did not pay: the connection never opened, or AT
  confirmed the transfer failed. Anything ambiguous (timeout after sending,
  5xx, crash mid-call) becomes 'unknown' for a human to reconcile with the AT
  dashboard. Duplicate status callbacks are no-ops.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Payout, Speaker
from .phones import decrypt_phone
from .sms.at_client import AUTH_RETRIES, AUTH_RETRY_DELAY, post_with_auth_retry

log = logging.getLogger(__name__)
LIVE_URL = "https://api.africastalking.com/version1/airtime/send"
SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/airtime/send"
MAX_ATTEMPTS = 3


@dataclass
class AirtimeResult:
    state: str  # sent | failed | retry | later | unknown
    request_id: str | None = None
    error: str | None = None


class AirtimeClient(Protocol):
    def send(self, phone: str, amount_kes: int) -> AirtimeResult: ...


def airtime_form(username: str, phone: str, amount_kes: int) -> dict:
    """Request body. AT docs were not reachable when this was written: check
    the recipients format on the first sandbox run."""
    return {"username": username, "recipients": json.dumps(
        [{"phoneNumber": phone, "amount": f"KES {amount_kes}"}])}


# AT refuses the same number + amount twice within 5 minutes and pays
# nothing (numSent 0), e.g. a speaker who finishes two tasks quickly. That
# payout waits and is sent again later; the wait doesn't use up attempts.
DUPLICATE_ERROR = "duplicate_within_5min"
DUPLICATE_WAIT = timedelta(minutes=5, seconds=30)


def parse_airtime_response(body: dict) -> AirtimeResult:
    rs = body.get("responses") or []
    err = str(body.get("errorMessage") or "")
    if not rs and "duplicate request" in err.lower() and not int(body.get("numSent") or 0):
        return AirtimeResult("later", None, DUPLICATE_ERROR)
    if not rs:
        return AirtimeResult("failed", None, str(body.get("errorMessage") or "no_response")[:100])
    r = rs[0]
    rid = r.get("requestId")
    rid = None if rid in (None, "", "None") else str(rid)
    status = str(r.get("status", ""))
    if status in ("Sent", "Success"):
        return AirtimeResult("sent", rid)
    err = r.get("errorMessage")
    return AirtimeResult("failed", rid, str(err if err not in (None, "None") else status)[:100])


class ATAirtimeClient:
    def __init__(self, username: str, api_key: str, sandbox: bool, timeout: float = 30.0,
                 auth_retries: int = AUTH_RETRIES, retry_delay: float = AUTH_RETRY_DELAY):
        self.username, self.api_key = username, api_key
        self.auth_retries, self.retry_delay = auth_retries, retry_delay
        self.url = SANDBOX_URL if sandbox else LIVE_URL
        self.http = httpx.Client(timeout=timeout)

    def send(self, phone: str, amount_kes: int) -> AirtimeResult:
        try:
            # A 401 means AT did nothing (no payment), so retrying it is safe.
            r = post_with_auth_retry(
                self.http, self.url, self.auth_retries, self.retry_delay,
                data=airtime_form(self.username, phone, amount_kes),
                headers={"apiKey": self.api_key, "Accept": "application/json"})
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return AirtimeResult("retry", error=type(e).__name__)  # never reached AT
        except httpx.HTTPError as e:
            return AirtimeResult("unknown", error=type(e).__name__)
        if r.status_code >= 500:
            return AirtimeResult("unknown", error=f"http_{r.status_code}")
        if r.status_code >= 400:
            return AirtimeResult("failed", error=f"http_{r.status_code}")
        return parse_airtime_response(r.json())


class FakeAirtimeClient:
    def __init__(self):
        self.sent: list[tuple[str, int]] = []
        self.next_results: list[AirtimeResult] = []

    def send(self, phone: str, amount_kes: int) -> AirtimeResult:
        if self.next_results:
            res = self.next_results.pop(0)
            if res.state == "sent":
                self.sent.append((phone, amount_kes))
            return res
        self.sent.append((phone, amount_kes))
        return AirtimeResult("sent", f"ATQid_fake_{len(self.sent)}")


def flush_payouts(db: Session, client: AirtimeClient, key: str,
                  clock: Callable[[], datetime]) -> int:
    rows = db.scalars(select(Payout).where(Payout.status == "pending")
                      .order_by(Payout.id)).all()
    n = 0
    for p in rows:
        if (p.error == DUPLICATE_ERROR and p.updated_at is not None
                and clock() - p.updated_at < DUPLICATE_WAIT):
            continue  # AT's 5-minute duplicate window: try again later
        p.status, p.attempts, p.updated_at = "sending", p.attempts + 1, clock()
        db.commit()  # recorded before the call
        sp = db.get(Speaker, p.speaker_id)
        try:
            res = client.send(decrypt_phone(sp.phone_enc, key), p.amount_kes)
        except Exception as e:  # noqa: BLE001
            res = AirtimeResult("unknown", error=type(e).__name__)
        p.at_transaction_id = res.request_id or p.at_transaction_id
        p.error, p.updated_at = res.error, clock()
        if res.state == "sent":
            p.status = "sent"
            n += 1
        elif res.state == "later":
            p.status, p.attempts = "pending", p.attempts - 1  # AT paid nothing
        elif res.state == "retry" and p.attempts < MAX_ATTEMPTS:
            p.status = "pending"
        elif res.state == "unknown":
            p.status = "unknown"
            log.error("payout %s outcome unknown (%s): reconcile manually", p.id, res.error)
        else:
            p.status = "failed"
        db.commit()  # recorded after the call
    return n


def airtime_status_callback(db: Session, request_id: str, status: str,
                            clock: Callable[[], datetime]) -> bool:
    """AT's airtime status callback. Only a 'sent' payout moves, so duplicate
    callbacks change nothing. A confirmed failure is re-queued (AT did not pay)."""
    p = db.scalar(select(Payout).where(Payout.at_transaction_id == request_id))
    if p is None or p.status != "sent":
        return False
    p.updated_at = clock()
    if status == "Success":
        p.status = "success"
        db.get(Speaker, p.speaker_id).airtime_paid_total += p.amount_kes
    elif status == "Failed":
        p.status = "pending" if p.attempts < MAX_ATTEMPTS else "failed"
        p.error = "callback_failed"
    else:
        return False
    return True
