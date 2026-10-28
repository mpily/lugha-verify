"""FastAPI app: AT callbacks, admin API, in-process scheduler."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from .config import Settings
from .db import Base, make_session_factory
from .models import Campaign, OutboundSms, ReviewItem, SourceText, Speaker
from .phones import encrypt_phone, hash_phone
from .review.engine import ReviewEngine
from .review.setup import CandidateIn, create_review_items
from .sms import templates as T
from .sms.at_client import ATClient, SmsClient
from .sms.outbox import DbOutbox, flush_outbox
from .translators.base import Translator
from .translators.default_api import DefaultApiTranslator

log = logging.getLogger(__name__)
TICK_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ schemas
class SpeakerIn(BaseModel):
    phone: str
    languages: list[str] = Field(min_length=1, description="most preferred first")
    trusted_for: list[str] = []
    source: Literal["customer", "network"] = "customer"


class SpeakerOut(BaseModel):
    id: int
    languages: list[str]
    trusted_for: list[str]
    source: str
    opted_in: bool
    opted_out: bool


class CandidateBody(BaseModel):
    text: str | None
    origin: Literal["default_api", "customer_model", "uploaded"] = "uploaded"
    model_ref: str | None = None


class SourceBody(BaseModel):
    text: str = Field(min_length=1)
    swahili_text: str | None = None
    candidates: dict[str, CandidateBody] = {}


class CampaignIn(BaseModel):
    mode: Literal["bulk", "research"] = "research"
    source_lang: Literal["eng", "swh"] = "eng"
    fallback_lang: Literal["eng", "swh"] = "eng"
    review_deadline_min: int | None = Field(None, gt=0)
    sources: list[SourceBody] = Field(min_length=1)


def create_app(settings: Settings | None = None, sms_client: SmsClient | None = None,
               clock: Callable[[], datetime] = utcnow,
               start_scheduler: bool = True,
               translator: Translator | None = None) -> FastAPI:
    settings = settings or Settings()  # fails fast on bad/missing env vars
    engine, Session = make_session_factory(settings.database_url)
    Base.metadata.create_all(engine)  # TODO(step): Alembic migrations
    sms_client = sms_client or ATClient(settings.at_username,
                                        settings.at_api_key.get_secret_value(),
                                        settings.at_sender, settings.at_sandbox)
    translator = translator or DefaultApiTranslator(settings.translate_api_url)
    key = settings.phone_enc_key.get_secret_value()
    salt = settings.phone_hash_salt.get_secret_value()
    lock = threading.RLock()  # one writer at a time (inbound vs scheduler)

    def run(fn):
        """Run fn(db, engine) in one transaction, then flush the outbox."""
        with lock, Session() as db:
            eng = ReviewEngine(db, settings, clock, DbOutbox(db, clock))
            out = fn(db, eng)
            db.commit()
            return out

    def flush():
        with lock, Session() as db:
            return flush_outbox(db, sms_client, key, clock)

    def tick():
        run(lambda db, eng: eng.tick())
        flush()

    def retranslate():
        """Re-make candidates from reviewed Swahili. The translator call can
        take minutes (Space asleep), so it runs outside the lock."""
        jobs = run(lambda db, eng: eng.pending_retranslations())
        for item_id, swahili, lang in jobs:
            try:
                res = translator.translate(swahili, "swh", lang)
                text, ref = res.text, res.model_ref
            except Exception:  # noqa: BLE001
                log.exception("re-translation failed")
                text, ref = None, None
            run(lambda db, eng: eng.apply_retranslation(item_id, swahili, text, ref))
        if jobs:
            flush()
        return len(jobs)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if start_scheduler:
            def every(fn, seconds):
                async def loop():
                    while True:
                        try:
                            await asyncio.to_thread(fn)
                        except Exception:  # noqa: BLE001
                            log.exception("%s failed", fn.__name__)
                        await asyncio.sleep(seconds)
                return asyncio.create_task(loop())
            tasks = [every(tick, TICK_SECONDS), every(retranslate, 10)]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Lugha Verify", lifespan=lifespan)
    app.state.settings, app.state.session, app.state.tick = settings, Session, tick
    app.state.flush, app.state.retranslate = flush, retranslate

    def check_callback(token: str | None = Query(None)):
        want = settings.at_callback_token
        if want is not None and not (token and hmac.compare_digest(
                token, want.get_secret_value())):
            raise HTTPException(403, "bad callback token")

    def check_admin(authorization: str | None = Header(None)):
        want = "Bearer " + settings.admin_token.get_secret_value()
        if not authorization or not hmac.compare_digest(authorization, want):
            raise HTTPException(401, "admin token required")

    @app.get("/health")
    def health():
        return {"ok": True, "at_sandbox": settings.at_sandbox}

    # ------------------------------------------------------- AT callbacks
    @app.post("/at/sms/inbound", dependencies=[Depends(check_callback)])
    def sms_inbound(bg: BackgroundTasks, from_: str = Form(..., alias="from"),
                    text: str = Form(""), id: str | None = Form(None)):
        try:
            h = hash_phone(from_, salt)
        except ValueError:
            return {"ok": False}
        run(lambda db, eng: eng.handle_inbound(h, text, at_id=id))
        bg.add_task(flush)
        return {"ok": True}

    @app.post("/at/sms/delivery", dependencies=[Depends(check_callback)])
    def sms_delivery(id: str = Form(...), status: str = Form(...),
                     failureReason: str | None = Form(None)):
        def upd(db, eng):
            row = db.scalar(select(OutboundSms).where(OutboundSms.at_message_id == id))
            if row is not None:
                row.delivery_status = status[:32]
                row.failure_reason = (failureReason or None) and failureReason[:64]
        run(upd)
        return {"ok": True}

    # ---------------------------------------------------------- admin API
    def speaker_out(sp: Speaker) -> SpeakerOut:
        return SpeakerOut(id=sp.id, languages=sp.languages, trusted_for=sp.trusted_for,
                          source=sp.source, opted_in=sp.opted_in_at is not None,
                          opted_out=sp.opted_out_at is not None)

    @app.post("/api/speakers", dependencies=[Depends(check_admin)])
    def add_speaker(body: SpeakerIn, bg: BackgroundTasks) -> SpeakerOut:
        try:
            h = hash_phone(body.phone, salt)
        except ValueError:
            raise HTTPException(422, "invalid phone number")

        def do(db, eng):
            sp = db.scalar(select(Speaker).where(Speaker.phone_hash == h))
            if sp is None:
                sp = Speaker(phone_hash=h, phone_enc=encrypt_phone(body.phone, key))
                db.add(sp)
            sp.languages = [x.lower() for x in body.languages]
            sp.trusted_for = [x.lower() for x in body.trusted_for]
            sp.source = body.source
            db.flush()
            if sp.opted_in_at is None and sp.opted_out_at is None:
                eng.outbox.send(sp, T.opt_in_prompt())
            return speaker_out(sp)
        out = run(do)
        bg.add_task(flush)
        return out

    @app.post("/api/campaigns", dependencies=[Depends(check_admin)])
    def create_campaign(body: CampaignIn):
        def do(db, eng):
            now = clock()
            mins = body.review_deadline_min or settings.review_deadline_min
            c = Campaign(mode=body.mode, source_lang=body.source_lang,
                         fallback_lang=body.fallback_lang, created_at=now,
                         review_deadline=now + timedelta(minutes=mins), status="draft")
            db.add(c)
            n = 0
            for sb in body.sources:
                st = SourceText(campaign=c, text=sb.text, swahili_text=sb.swahili_text)
                db.add(st)
                n += len(create_review_items(db, settings, st, {
                    lang.lower(): CandidateIn(cb.text, cb.origin, cb.model_ref)
                    for lang, cb in sb.candidates.items()}))
            db.flush()
            return {"id": c.id, "review_items": n}
        return run(do)

    def items_of(db, cid):
        return db.scalars(select(ReviewItem).join(ReviewItem.candidate)
                          .join(SourceText).where(SourceText.campaign_id == cid)).all()

    @app.post("/api/campaigns/{cid}/start", dependencies=[Depends(check_admin)])
    def start_campaign(cid: int, bg: BackgroundTasks):
        def do(db, eng):
            c = db.get(Campaign, cid)
            if c is None:
                raise HTTPException(404, "no such campaign")
            if c.status != "draft":
                raise HTTPException(409, f"campaign is {c.status}")
            # the review window starts now, not at creation
            c.review_deadline = clock() + (c.review_deadline - c.created_at)
            c.status = "running"
            eng.start_items(list(items_of(db, cid)))
            return {"id": cid, "status": c.status}
        out = run(do)
        bg.add_task(flush)
        return out

    @app.get("/api/campaigns/{cid}", dependencies=[Depends(check_admin)])
    def campaign_status(cid: int):
        with Session() as db:
            c = db.get(Campaign, cid)
            if c is None:
                raise HTTPException(404, "no such campaign")
            items = items_of(db, cid)
            return {"id": cid, "status": c.status, "review_deadline": c.review_deadline,
                    "items": [{"id": i.id, "lang": i.lang, "state": i.state,
                               "outcome": i.outcome, "source_id": i.candidate.source_id,
                               "tasks": {s: sum(t.status == s for t in i.tasks)
                                         for s in sorted({t.status for t in i.tasks})}}
                              for i in items]}

    return app


def _default_app():  # `uvicorn app.main:app`
    logging.basicConfig(level=logging.INFO)
    return create_app()


def __getattr__(name):  # build lazily so importing the module needs no env vars
    if name == "app":
        return _default_app()
    raise AttributeError(name)
