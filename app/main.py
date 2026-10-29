"""FastAPI app: AT callbacks, admin REST API, customer portal, scheduler."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Callable

from fastapi import (BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query,
                     Request)
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select

from .bulk import finish_campaigns, import_recipients, recipient_keyword
from .config import Settings
from .db import ensure_schema, make_session_factory
from .exports import (HttpSink, LocalJsonlSink, SharedDataSink, campaign_rows, export_shared,
                      to_csv, to_jsonl)
from .models import Delivery, OutboundSms, Speaker
from .payouts import ATAirtimeClient, AirtimeClient, airtime_status_callback, flush_payouts
from .phones import hash_phone
from .reports import campaign_report, report_csv
from .review.engine import ReviewEngine
from .schemas import CampaignIn, SpeakerIn, SpeakerOut
from .services import (ServiceError, add_speaker, campaign_status, create_campaign,
                       start_campaign)
from .sms.at_client import ATClient, SmsClient
from .sms.outbox import DbOutbox, flush_outbox
from .sms.parser import Keyword, parse_sms
from .translators.base import Translator
from .translators.default_api import DefaultApiTranslator
from .web.portal import make_router

log = logging.getLogger(__name__)
TICK_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def create_app(settings: Settings | None = None, sms_client: SmsClient | None = None,
               clock: Callable[[], datetime] = utcnow,
               start_scheduler: bool = True,
               translator: Translator | None = None,
               airtime_client: AirtimeClient | None = None,
               shared_sink: SharedDataSink | None = None) -> FastAPI:
    settings = settings or Settings()  # fails fast on bad/missing env vars
    engine, Session = make_session_factory(settings.database_url)
    ensure_schema(engine)
    sms_client = sms_client or ATClient(settings.at_username,
                                        settings.at_api_key.get_secret_value(),
                                        settings.at_sender, settings.at_sandbox)
    translator = translator or DefaultApiTranslator(settings.translate_api_url)
    airtime_client = airtime_client or ATAirtimeClient(
        settings.at_username, settings.at_api_key.get_secret_value(), settings.at_sandbox)
    shared_sink = shared_sink or (HttpSink(settings.shared_data_url)
                                  if settings.shared_data_url else LocalJsonlSink())
    key = settings.phone_enc_key.get_secret_value()
    salt = settings.phone_hash_salt.get_secret_value()
    lock = threading.RLock()  # one writer at a time (requests vs scheduler)

    def run(fn):
        """Run fn(db, engine) in one transaction (rolled back if fn raises)."""
        with lock, Session() as db:
            eng = ReviewEngine(db, settings, clock, DbOutbox(db, clock))
            out = fn(db, eng)
            db.commit()
            return out

    def flush():
        with lock, Session() as db:
            n = flush_outbox(db, sms_client, key, clock)
            flush_payouts(db, airtime_client, key, clock)
            return n

    def tick():
        def do(db, eng):
            eng.tick()
            finish_campaigns(db, settings, eng.outbox, clock)
        run(do)
        flush()

    def draft():
        """First machine drafts for items in 'drafting' (outside the lock;
        the Space may take minutes to wake). No result -> no candidate."""
        jobs = run(lambda db, eng: eng.pending_drafts(translator.supports))
        for item_id, src, text, lang in jobs:
            res_text = ref = None
            if src is not None:
                try:
                    res = translator.translate(text, src, lang)
                    res_text, ref = res.text, res.model_ref
                except Exception:  # noqa: BLE001
                    log.exception("draft translation failed")
            run(lambda db, eng: eng.apply_draft(item_id, res_text, ref))
        if jobs:
            flush()
        return len(jobs)

    def machine_jobs():
        return draft() + retranslate()

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

    def share():
        return export_shared(run, shared_sink, salt, clock)

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
            tasks = [every(tick, TICK_SECONDS), every(machine_jobs, 10), every(share, 300)]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Lugha Verify", lifespan=lifespan)
    app.state.settings, app.state.session, app.state.tick = settings, Session, tick
    app.state.flush, app.state.retranslate, app.state.share = flush, retranslate, share
    app.state.draft = draft

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return PlainTextResponse(exc.message, status_code=exc.status)

    def check_callback(token: str | None = Query(None)):
        want = settings.at_callback_token
        if want is not None and not (token and hmac.compare_digest(
                token, want.get_secret_value())):
            raise HTTPException(403, "bad callback token")

    def check_admin(authorization: str | None = Header(None)):
        want = "Bearer " + settings.admin_token.get_secret_value()
        if not authorization or not hmac.compare_digest(authorization, want):
            raise HTTPException(401, "admin token required")

    @app.get("/")
    def root():
        return RedirectResponse("/portal", status_code=303)

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

        def do(db, eng):
            is_speaker = db.scalar(select(Speaker.id).where(Speaker.phone_hash == h))
            eng.handle_inbound(h, text, at_id=id)
            parsed = parse_sms(text)
            if is_speaker is None and isinstance(parsed, Keyword):
                recipient_keyword(db, h, parsed.word, eng.outbox, clock)
        run(do)
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
                if row.delivery_id is not None:
                    db.get(Delivery, row.delivery_id).delivery_status = status[:32]
        run(upd)
        return {"ok": True}

    @app.post("/at/airtime/status", dependencies=[Depends(check_callback)])
    def airtime_status(requestId: str = Form(...), status: str = Form(...)):
        changed = run(lambda db, eng: airtime_status_callback(db, requestId, status, clock))
        return {"ok": True, "changed": changed}

    # ---------------------------------------------------------- admin API
    admin = [Depends(check_admin)]

    @app.post("/api/speakers", dependencies=admin)
    def api_add_speaker(body: SpeakerIn, bg: BackgroundTasks) -> SpeakerOut:
        out = run(lambda db, eng: add_speaker(db, eng, settings, body))
        bg.add_task(flush)
        return out

    @app.post("/api/lists/{list_id}/recipients", dependencies=admin)
    async def api_upload_recipients(list_id: str, request: Request):
        """Body: CSV text, columns `phone, preferred_lang`."""
        if not (1 <= len(list_id) <= 64):
            raise HTTPException(422, "bad list id")
        body = (await request.body()).decode("utf-8-sig", errors="replace")
        return await asyncio.to_thread(
            run, lambda db, eng: import_recipients(db, settings, list_id, body))

    @app.post("/api/campaigns", dependencies=admin)
    def api_create_campaign(body: CampaignIn):
        return run(lambda db, eng: create_campaign(db, settings, clock, body))

    @app.post("/api/campaigns/{cid}/start", dependencies=admin)
    def api_start_campaign(cid: int, bg: BackgroundTasks):
        out = run(lambda db, eng: start_campaign(db, eng, settings, clock, cid,
                                                  translator.supports))
        bg.add_task(flush)
        return out

    @app.get("/api/campaigns/{cid}", dependencies=admin)
    def api_campaign_status(cid: int):
        return run(lambda db, eng: campaign_status(db, cid))

    @app.get("/api/campaigns/{cid}/export", dependencies=admin)
    def api_export(cid: int, format: str = Query("jsonl", pattern="^(csv|jsonl)$")):
        run(lambda db, eng: campaign_status(db, cid))  # 404 if missing
        rows = run(lambda db, eng: campaign_rows(db, cid))
        return PlainTextResponse(to_csv(rows) if format == "csv" else to_jsonl(rows),
                                 media_type="text/csv" if format == "csv"
                                 else "application/x-ndjson")

    @app.get("/api/campaigns/{cid}/report", dependencies=admin)
    def api_report(cid: int, format: str = Query("json", pattern="^(json|csv)$")):
        run(lambda db, eng: campaign_status(db, cid))  # 404 if missing
        rep = run(lambda db, eng: campaign_report(db, cid))
        if format == "csv":
            return PlainTextResponse(report_csv(rep), media_type="text/csv")
        return rep

    # ------------------------------------------------------------- portal
    app.include_router(make_router(SimpleNamespace(
        run=run, flush=flush, settings=settings, clock=clock, session=Session,
        supports=translator.supports)))
    return app


def _default_app():  # `uvicorn app.main:app`
    logging.basicConfig(level=logging.INFO)
    return create_app()


def __getattr__(name):  # build lazily so importing the module needs no env vars
    if name == "app":
        return _default_app()
    raise AttributeError(name)
