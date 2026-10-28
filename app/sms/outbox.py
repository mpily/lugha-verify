"""SMS outbox: queue in the DB, send after commit."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import OutboundSms, Speaker
from ..phones import decrypt_phone
from .at_client import SmsClient
from .segments import count_segments

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class DbOutbox:
    def __init__(self, db: Session, clock: Callable[[], datetime]):
        self.db, self.clock = db, clock

    def send(self, speaker: Speaker, text: str) -> None:
        self.db.add(OutboundSms(speaker_id=speaker.id, phone_enc=speaker.phone_enc,
                                text=text, created_at=self.clock(),
                                segments=count_segments(text)[1]))


def flush_outbox(db: Session, client: SmsClient, key: str,
                 clock: Callable[[], datetime], limit: int = 200) -> int:
    """Send queued SMS. Each row is marked 'sending' and committed before the
    AT call, so a crash can't cause a double send (it leaves the row in
    'sending' for a human to check instead). Returns the number sent."""
    rows = db.scalars(select(OutboundSms).where(OutboundSms.status == "queued")
                      .order_by(OutboundSms.id).limit(limit)).all()
    n = 0
    for row in rows:
        row.status, row.attempts = "sending", row.attempts + 1
        db.commit()
        try:
            res = client.send(decrypt_phone(row.phone_enc, key), row.text)
        except Exception as e:  # noqa: BLE001  network error: retry later
            row.status = "queued" if row.attempts < MAX_ATTEMPTS else "failed"
            row.at_status = type(e).__name__[:64]
            log.warning("sms %s send error %s (attempt %d)", row.id,
                        type(e).__name__, row.attempts)
            db.commit()
            continue
        row.at_status, row.at_message_id, row.cost = res.status, res.message_id, res.cost
        row.status = "sent" if res.ok else "failed"
        row.sent_at = clock()
        n += res.ok
        db.commit()
    return n
