"""Bulk mode: recipient lists, campaign completion, delivery (CLAUDE.md §3, §5)."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .delivery import SentenceInfo, compose_message
from .models import (Campaign, Candidate, Delivery, Recipient, ReviewItem, SourceText,
                     Translation)
from .phones import encrypt_phone, hash_phone
from .sms.outbox import DbOutbox

LANG_RE_OK = set("abcdefghijklmnopqrstuvwxyz")


def import_recipients(db: Session, settings: Settings, list_id: str, csv_text: str) -> dict:
    """CSV `phone, preferred_lang` (header optional). Re-uploading a number in
    the same list updates its language. Invalid rows are reported by line
    number only (never echo phone numbers)."""
    salt = settings.phone_hash_salt.get_secret_value()
    key = settings.phone_enc_key.get_secret_value()
    added = updated = 0
    invalid: list[int] = []
    for n, row in enumerate(csv.reader(io.StringIO(csv_text)), start=1):
        if not row or not "".join(row).strip():
            continue
        if n == 1 and row[0].strip().lower() in ("phone", "phone_number", "msisdn"):
            continue
        if len(row) < 2:
            invalid.append(n)
            continue
        lang = row[1].strip().lower()
        if not (2 <= len(lang) <= 8 and set(lang) <= LANG_RE_OK):
            invalid.append(n)
            continue
        try:
            h = hash_phone(row[0], salt)
        except ValueError:
            invalid.append(n)
            continue
        r = db.scalar(select(Recipient).where(Recipient.list_id == list_id,
                                              Recipient.phone_hash == h))
        if r is None:
            db.add(Recipient(list_id=list_id, phone_hash=h, preferred_lang=lang,
                             phone_enc=encrypt_phone(row[0], key)))
            db.flush()
            added += 1
        else:
            r.preferred_lang = lang
            updated += 1
    return {"list_id": list_id, "added": added, "updated": updated, "invalid_lines": invalid}


def list_languages(db: Session, list_id: str) -> set[str]:
    return set(db.scalars(select(Recipient.preferred_lang)
                          .where(Recipient.list_id == list_id)).all())


def recipient_keyword(db: Session, phone_hash: str, word: str, outbox: DbOutbox,
                      clock: Callable[[], datetime]) -> None:
    """STOP / YES from a recipient (not a speaker): opt out of / back into
    every list."""
    rows = db.scalars(select(Recipient).where(Recipient.phone_hash == phone_hash)).all()
    if not rows or word not in ("STOP", "YES"):
        return
    for r in rows:
        r.opted_out_at = clock() if word == "STOP" else None
    outbox.send_phone(rows[0].phone_enc, "You will get no more messages from this sender. "
                      "Reply YES to rejoin." if word == "STOP" else
                      "You will receive messages again. Reply STOP to leave.")


def _items(db: Session, campaign_id: int) -> list[ReviewItem]:
    return list(db.scalars(select(ReviewItem).join(Candidate).join(SourceText)
                           .where(SourceText.campaign_id == campaign_id)).all())


def _translation(db: Session, source_id: int, lang: str) -> Translation | None:
    return db.scalar(select(Translation).where(Translation.source_id == source_id,
                                               Translation.lang == lang))


def deliver(db: Session, settings: Settings, c: Campaign, outbox: DbOutbox,
            clock: Callable[[], datetime]) -> int:
    """Queue one SMS per recipient. Safe to call twice (one delivery per
    recipient per campaign)."""
    sources = list(db.scalars(select(SourceText).where(SourceText.campaign_id == c.id)
                              .order_by(SourceText.id)).all())
    done = set(db.scalars(select(Delivery.recipient_id)
                          .where(Delivery.campaign_id == c.id)).all())
    cache: dict[str, list[SentenceInfo]] = {}

    def sentences(lang: str) -> list[SentenceInfo]:
        if lang not in cache:
            out = []
            for st in sources:
                sw = _translation(db, st.id, "swh")
                tr = _translation(db, st.id, lang)
                out.append(SentenceInfo(
                    source_text=st.text, customer_swahili=st.swahili_text,
                    verified_swahili=sw.final_text if sw and sw.status == "verified" else None,
                    translation_text=tr.final_text if tr else None,
                    translation_status=tr.status if tr else None))
            cache[lang] = out
        return cache[lang]

    n = 0
    recips = db.scalars(select(Recipient).where(Recipient.list_id == c.list_id)
                        .order_by(Recipient.id)).all()
    for r in recips:
        if r.id in done or r.opted_out_at is not None:
            continue
        msg = compose_message(recipient_lang=r.preferred_lang, source_lang=c.source_lang,
                              fallback_lang=c.fallback_lang,
                              sentences=sentences(r.preferred_lang),
                              needs_review_for=settings.needs_review_for)
        d = Delivery(campaign_id=c.id, recipient_id=r.id, lang_sent=msg.lang,
                     text_sent=msg.text, was_fallback=msg.was_fallback,
                     delivery_status="queued")
        db.add(d)
        db.flush()
        outbox.send_phone(r.phone_enc, msg.text, delivery_id=d.id)
        n += 1
    return n


def finish_campaigns(db: Session, settings: Settings, outbox: DbOutbox,
                     clock: Callable[[], datetime]) -> None:
    """When every review item of a running campaign is closed (the engine
    closes them at the deadline at the latest): deliver (bulk) or finish
    (research)."""
    for c in db.scalars(select(Campaign).where(Campaign.status == "running")).all():
        if any(i.state != "closed" for i in _items(db, c.id)):
            continue
        if c.mode == "bulk":
            deliver(db, settings, c, outbox, clock)
            c.status = "delivered"
        else:
            c.status = "done"
