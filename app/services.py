"""Customer actions, shared by the REST API and the portal."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .bulk import finish_campaigns, list_languages
from .config import Settings
from .models import (Campaign, Candidate, Delivery, Payout, Recipient, ReviewItem,
                     ReviewTask, SourceText, Speaker, Translation)
from .phones import encrypt_phone, hash_phone
from .review.engine import ReviewEngine
from .review.setup import CandidateIn, create_review_items
from .schemas import CampaignIn, SourceBody, SpeakerIn, SpeakerOut
from .sms import templates as T


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def sources_from_message(message: str, swahili: str | None) -> list[SourceBody]:
    """Split a bulk message into sentences for review. A customer Swahili
    version is split too; if the sentence counts differ the texts can't be
    paired, so the whole message is reviewed as one unit."""
    en = split_sentences(message)
    sw = split_sentences(swahili) if swahili and swahili.strip() else None
    if sw is not None and len(sw) != len(en):
        return [SourceBody(text=message.strip(), swahili_text=swahili.strip())]
    return [SourceBody(text=e, swahili_text=sw[i] if sw else None) for i, e in enumerate(en)]


def speaker_out(sp: Speaker) -> SpeakerOut:
    return SpeakerOut(id=sp.id, languages=sp.languages, trusted_for=sp.trusted_for,
                      source=sp.source, opted_in=sp.opted_in_at is not None,
                      opted_out=sp.opted_out_at is not None)


def add_speaker(db: Session, eng: ReviewEngine, settings: Settings, body: SpeakerIn) -> SpeakerOut:
    try:
        h = hash_phone(body.phone, settings.phone_hash_salt.get_secret_value())
    except ValueError:
        raise ServiceError(422, "invalid phone number")
    langs = [x.strip().lower() for x in body.languages if x.strip()]
    if not langs:
        raise ServiceError(422, "at least one language")
    sp = db.scalar(select(Speaker).where(Speaker.phone_hash == h))
    if sp is None:
        sp = Speaker(phone_hash=h, phone_enc=encrypt_phone(
            body.phone, settings.phone_enc_key.get_secret_value()))
        db.add(sp)
    sp.languages = langs
    sp.trusted_for = [x.strip().lower() for x in body.trusted_for if x.strip()]
    sp.source = body.source
    db.flush()
    if sp.opted_in_at is None and sp.opted_out_at is None:
        eng.outbox.send(sp, T.opt_in_prompt())
    return speaker_out(sp)


def create_campaign(db: Session, settings: Settings, clock: Callable[[], datetime],
                    body: CampaignIn) -> dict:
    now = clock()
    needed: set[str] = set()
    if body.mode == "bulk":
        langs = list_languages(db, body.list_id) if body.list_id else set()
        if not langs:
            raise ServiceError(422, "bulk mode needs the id of an uploaded recipient list")
        needed = {x for x in langs if x != body.source_lang
                  and settings.needs_review_for(x, False)}
    mins = body.review_deadline_min or settings.review_deadline_min
    c = Campaign(mode=body.mode, source_lang=body.source_lang,
                 fallback_lang=body.fallback_lang, created_at=now,
                 review_deadline=now + timedelta(minutes=mins), status="draft",
                 list_id=body.list_id)
    db.add(c)
    n = 0
    for sb in body.sources:
        st = SourceText(campaign=c, text=sb.text, swahili_text=sb.swahili_text)
        db.add(st)
        cands = {lang.lower(): CandidateIn(cb.text, cb.origin, cb.model_ref)
                 for lang, cb in sb.candidates.items()}
        want = set(needed)
        if want - {"swh"} and body.source_lang != "swh" and not sb.swahili_text:
            want.add("swh")  # pivot for reviewers who don't read the source
        for lang in want - set(cands):
            # no candidate yet: speakers translate from scratch
            # (TODO step 5: ask the translator first)
            cands[lang] = CandidateIn(None, "default_api", None)
        n += len(create_review_items(db, settings, st, cands))
    db.flush()
    return {"id": c.id, "review_items": n}


def items_of(db: Session, cid: int) -> list[ReviewItem]:
    return list(db.scalars(select(ReviewItem).join(Candidate).join(SourceText)
                           .where(SourceText.campaign_id == cid)
                           .order_by(SourceText.id, ReviewItem.lang)).all())


def start_campaign(db: Session, eng: ReviewEngine, settings: Settings,
                   clock: Callable[[], datetime], cid: int) -> dict:
    c = db.get(Campaign, cid)
    if c is None:
        raise ServiceError(404, "no such campaign")
    if c.status != "draft":
        raise ServiceError(409, f"campaign is {c.status}")
    c.review_deadline = clock() + (c.review_deadline - c.created_at)  # window starts now
    c.status = "running"
    eng.start_items(items_of(db, cid))
    finish_campaigns(db, settings, eng.outbox, clock)  # nothing to review
    return {"id": cid, "status": c.status}


def campaign_status(db: Session, cid: int) -> dict:
    c = db.get(Campaign, cid)
    if c is None:
        raise ServiceError(404, "no such campaign")
    items = []
    for i in items_of(db, cid):
        tr = db.scalar(select(Translation).where(
            Translation.source_id == i.candidate.source_id, Translation.lang == i.lang))
        items.append({
            "id": i.id, "lang": i.lang, "state": i.state, "outcome": i.outcome,
            "source_id": i.candidate.source_id, "source_text": i.candidate.source.text,
            "candidate": i.candidate.text,
            "final_text": tr.final_text if tr else None,
            "final_status": tr.status if tr else None,
            "tasks": {s: sum(t.status == s for t in i.tasks)
                      for s in sorted({t.status for t in i.tasks})}})
    summary: dict[str, dict] = {}
    for d in db.scalars(select(Delivery).where(Delivery.campaign_id == cid)):
        k = summary.setdefault(d.lang_sent, {"n": 0, "fallback": 0, "delivered": 0})
        k["n"] += 1
        k["fallback"] += d.was_fallback
        k["delivered"] += d.delivery_status == "Success"
    paid = db.scalar(
        select(func.coalesce(func.sum(Payout.amount_kes), 0))
        .join(ReviewTask, Payout.review_task_id == ReviewTask.id)
        .join(ReviewItem).join(Candidate).join(SourceText)
        .where(SourceText.campaign_id == cid,
               Payout.status.in_(("pending", "sending", "sent", "success", "unknown"))))
    return {"id": cid, "mode": c.mode, "status": c.status, "list_id": c.list_id,
            "source_lang": c.source_lang, "fallback_lang": c.fallback_lang,
            "created_at": c.created_at, "review_deadline": c.review_deadline,
            "airtime_kes": paid, "deliveries": summary, "items": items}


def overview(db: Session) -> dict:
    lists = db.execute(select(Recipient.list_id, func.count(), func.count(Recipient.opted_out_at))
                       .group_by(Recipient.list_id).order_by(Recipient.list_id)).all()
    speakers = db.scalars(select(Speaker).order_by(Speaker.id)).all()
    campaigns = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    return {"lists": [{"id": l, "n": n, "opted_out": o} for l, n, o in lists],
            "speakers": speakers, "campaigns": campaigns}
