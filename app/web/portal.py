"""Customer portal: server-rendered, no JavaScript, works on a phone over 2G.

Auth: the ADMIN_TOKEN, typed once, sets an HttpOnly SameSite=Strict cookie
holding an HMAC of the token (never the token itself). Every POST form also
carries a CSRF value derived the same way.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ..bulk import import_recipients
from ..exports import campaign_rows, to_csv, to_jsonl
from ..reports import campaign_report, report_csv
from ..schemas import CampaignIn, CandidateBody, SourceBody, SpeakerIn
from ..services import (ServiceError, add_speaker, campaign_status, create_campaign,
                        overview, sources_from_message, start_campaign)

COOKIE = "lv_session"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_UPLOAD = 2_000_000  # bytes


def _mac(secret: str, purpose: str) -> str:
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def parse_research_csv(text: str, model_ref: str | None) -> list[SourceBody]:
    """Rows `source, lang, candidate` (header optional). Sources keep their
    first-seen order; an empty candidate means speakers translate from scratch."""
    order: dict[str, dict[str, CandidateBody]] = {}
    for n, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or not "".join(row).strip():
            continue
        if n == 1 and row[0].strip().lower() == "source":
            continue
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            raise ServiceError(422, f"line {n}: need source and lang")
        cand = row[2].strip() if len(row) > 2 and row[2].strip() else None
        order.setdefault(row[0].strip(), {})[row[1].strip().lower()] = CandidateBody(
            text=cand, origin="uploaded", model_ref=model_ref or None)
    if not order:
        raise ServiceError(422, "no rows")
    return [SourceBody(text=s, candidates=c) for s, c in order.items()]


async def _read_upload(form, field_file="file", field_text="csv_text") -> str:
    f = form.get(field_file)
    if isinstance(f, UploadFile) and f.filename:
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise ServiceError(413, "file too large (max 2 MB)")
        return data.decode("utf-8-sig", errors="replace")
    return str(form.get(field_text) or "")


def make_router(ctx) -> APIRouter:
    """ctx: run, flush, settings, clock, session (see main.create_app)."""
    r = APIRouter(prefix="/portal")
    token = ctx.settings.admin_token.get_secret_value()
    session_mac, csrf = _mac(token, "portal-session-v1"), _mac(token, "portal-csrf-v1")

    def authed(req: Request) -> bool:
        return hmac.compare_digest(req.cookies.get(COOKIE, ""), session_mac)

    def page(req: Request, name: str, status: int = 200, **kw):
        return TEMPLATES.TemplateResponse(req, name, {"authed": authed(req), "csrf": csrf, **kw},
                                          status_code=status)

    def login_redirect():
        return RedirectResponse("/portal/login", status_code=303)

    async def checked_form(req: Request):
        form = await req.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), csrf):
            raise ServiceError(403, "form expired, reload the page")
        return form

    def lists():
        return ctx.run(lambda db, eng: overview(db))["lists"]

    @r.get("/login", response_class=HTMLResponse)
    def login_form(req: Request):
        return page(req, "login.html")

    @r.post("/login")
    async def login(req: Request):
        form = await req.form()
        if not hmac.compare_digest(str(form.get("token", "")), token):
            return page(req, "login.html", 401, error="Wrong token.")
        resp = RedirectResponse("/portal", status_code=303)
        resp.set_cookie(COOKIE, session_mac, httponly=True, samesite="strict",
                        secure=req.url.scheme == "https", max_age=7 * 24 * 3600)
        return resp

    @r.post("/logout")
    async def logout(req: Request):
        resp = login_redirect()
        resp.delete_cookie(COOKIE)
        return resp

    @r.get("", response_class=HTMLResponse)
    def home(req: Request):
        if not authed(req):
            return login_redirect()
        return page(req, "home.html", **ctx.run(lambda db, eng: overview(db)))

    # ---------------------------------------------------------------- lists
    @r.get("/lists", response_class=HTMLResponse)
    def lists_page(req: Request):
        if not authed(req):
            return login_redirect()
        return page(req, "lists.html", lists=lists())

    @r.post("/lists", response_class=HTMLResponse)
    async def lists_upload(req: Request):
        if not authed(req):
            return login_redirect()
        try:
            form = await checked_form(req)
            list_id = str(form.get("list_id", "")).strip()
            if not (1 <= len(list_id) <= 64 and list_id.replace("-", "").replace("_", "").isalnum()):
                raise ServiceError(422, "list name: letters, digits, - and _ only")
            text = await _read_upload(form)
            if not text.strip():
                raise ServiceError(422, "no CSV given")
            res = ctx.run(lambda db, eng: import_recipients(db, ctx.settings, list_id, text))
        except ServiceError as e:
            return page(req, "lists.html", e.status, error=e.message, lists=lists())
        return page(req, "lists.html", result=res, lists=lists(), list_id=list_id)

    # ------------------------------------------------------------- speakers
    def speakers():
        return ctx.run(lambda db, eng: overview(db))["speakers"]

    @r.get("/speakers", response_class=HTMLResponse)
    def speakers_page(req: Request):
        if not authed(req):
            return login_redirect()
        return page(req, "speakers.html", speakers=speakers())

    @r.post("/speakers", response_class=HTMLResponse)
    async def speakers_add(req: Request):
        if not authed(req):
            return login_redirect()
        try:
            form = await checked_form(req)
            body = SpeakerIn(phone=str(form.get("phone", "")),
                             languages=str(form.get("languages", "")).replace(";", ",").split(","),
                             trusted_for=[x for x in str(form.get("trusted_for", "")).split(",")
                                          if x.strip()])
            ctx.run(lambda db, eng: add_speaker(db, eng, ctx.settings, body))
            ctx.flush()
        except ValidationError:
            return page(req, "speakers.html", 422, error="Check the phone and languages.",
                        speakers=speakers())
        except ServiceError as e:
            return page(req, "speakers.html", e.status, error=e.message, speakers=speakers())
        return page(req, "speakers.html", notice="Speaker added; opt-in SMS sent.",
                    speakers=speakers())

    # ------------------------------------------------------------ campaigns
    @r.get("/campaigns/new", response_class=HTMLResponse)
    def new_campaign(req: Request):
        if not authed(req):
            return login_redirect()
        return page(req, "campaign_new.html", lists=lists(),
                    default_deadline=ctx.settings.review_deadline_min)

    @r.post("/campaigns")
    async def create(req: Request):
        if not authed(req):
            return login_redirect()
        try:
            form = await checked_form(req)
            mode = form.get("mode")
            minutes = int(form.get("review_deadline_min") or ctx.settings.review_deadline_min)
            if mode == "bulk":
                message = str(form.get("message", ""))
                if not message.strip():
                    raise ServiceError(422, "message is empty")
                body = CampaignIn(mode="bulk", source_lang=form.get("source_lang", "eng"),
                                  fallback_lang=form.get("fallback_lang", "eng"),
                                  list_id=form.get("list_id"), review_deadline_min=minutes,
                                  sources=sources_from_message(message, form.get("swahili")))
            elif mode == "research":
                text = await _read_upload(form)
                body = CampaignIn(mode="research", source_lang=form.get("source_lang", "eng"),
                                  review_deadline_min=minutes,
                                  sources=parse_research_csv(text, form.get("model_ref")))
            else:
                raise ServiceError(422, "unknown mode")
            out = ctx.run(lambda db, eng: create_campaign(db, ctx.settings, ctx.clock, body))
        except (ValidationError, ValueError):
            return page(req, "campaign_new.html", 422, error="Check the form values.",
                        lists=lists(), default_deadline=ctx.settings.review_deadline_min)
        except ServiceError as e:
            return page(req, "campaign_new.html", e.status, error=e.message, lists=lists(),
                        default_deadline=ctx.settings.review_deadline_min)
        return RedirectResponse(f"/portal/campaigns/{out['id']}", status_code=303)

    @r.get("/campaigns/{cid}", response_class=HTMLResponse)
    def campaign(req: Request, cid: int):
        if not authed(req):
            return login_redirect()
        try:
            c = ctx.run(lambda db, eng: campaign_status(db, cid))
        except ServiceError as e:
            return page(req, "home.html", e.status, error=e.message,
                        **ctx.run(lambda db, eng: overview(db)))
        rep = ctx.run(lambda db, eng: campaign_report(db, cid))
        return page(req, "campaign.html", c=c, rep=rep)

    @r.post("/campaigns/{cid}/start")
    async def start(req: Request, cid: int):
        if not authed(req):
            return login_redirect()
        try:
            await checked_form(req)
            ctx.run(lambda db, eng: start_campaign(db, eng, ctx.settings, ctx.clock, cid,
                                                   ctx.supports))
            ctx.flush()
        except ServiceError as e:
            c = ctx.run(lambda db, eng: campaign_status(db, cid))
            rep = ctx.run(lambda db, eng: campaign_report(db, cid))
            return page(req, "campaign.html", e.status, error=e.message, c=c, rep=rep)
        return RedirectResponse(f"/portal/campaigns/{cid}", status_code=303)

    @r.get("/campaigns/{cid}/export.{fmt:path}")
    def export(req: Request, cid: int, fmt: str):
        if not authed(req):
            return login_redirect()
        if fmt == "report.csv":
            body = report_csv(ctx.run(lambda db, eng: campaign_report(db, cid)))
            return PlainTextResponse(body, media_type="text/csv", headers={
                "Content-Disposition": f'attachment; filename="campaign-{cid}-report.csv"'})
        if fmt not in ("csv", "jsonl"):
            return PlainTextResponse("csv or jsonl", status_code=404)
        rows = ctx.run(lambda db, eng: campaign_rows(db, cid))
        body = to_csv(rows) if fmt == "csv" else to_jsonl(rows)
        media = "text/csv" if fmt == "csv" else "application/x-ndjson"
        return PlainTextResponse(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="campaign-{cid}.{fmt}"'})

    return r
