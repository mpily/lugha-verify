"""Exports (CLAUDE.md §6, §7).

- Customer export: everything about the customer's own campaign. No phone
  numbers (the instance never shows them); speakers appear by id.
- Shared export: ONLY tasks whose speaker typed SHARE (share_consent), with
  no phone, no speaker id and no campaign id. It leaves the instance only if
  the customer set SHARED_DATA_URL; otherwise it goes to a local JSONL file.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Candidate, Payout, ReviewItem, ReviewTask, SourceText, Translation

log = logging.getLogger(__name__)

EXPORT_FIELDS = [
    "campaign_id", "source_id", "source_text", "lang", "item_state", "item_outcome",
    "final_text", "final_status", "task_id", "speaker_id", "speaker_source", "wave",
    "shown_source_lang", "shown_source_text", "candidate_text", "candidate_origin",
    "model_ref", "sent_at", "replied_at", "stars", "corrected_text", "task_status",
    "share_consent", "paid_kes", "payout_status"]


def _iso(d):
    return d.isoformat() if d else None


def campaign_rows(db: Session, cid: int) -> list[dict]:
    """One row per review task; items reviewed with no tasks (translation
    memory) get one row with empty task fields."""
    items = db.scalars(select(ReviewItem).join(Candidate).join(SourceText)
                       .where(SourceText.campaign_id == cid)
                       .order_by(SourceText.id, ReviewItem.lang)).all()
    out = []
    for i in items:
        src = i.candidate.source
        tr = db.scalar(select(Translation).where(Translation.source_id == src.id,
                                                 Translation.lang == i.lang))
        base = {"campaign_id": cid, "source_id": src.id, "source_text": src.text,
                "lang": i.lang, "item_state": i.state, "item_outcome": i.outcome,
                "final_text": tr.final_text if tr else None,
                "final_status": tr.status if tr else None,
                "candidate_text": i.candidate.text, "candidate_origin": i.candidate.origin,
                "model_ref": i.candidate.model_ref}
        tasks = sorted(i.tasks, key=lambda t: t.id)
        if not tasks:
            out.append({k: base.get(k) for k in EXPORT_FIELDS})
        for t in tasks:
            p = db.scalar(select(Payout).where(Payout.review_task_id == t.id))
            row = dict(base, task_id=t.id, speaker_id=t.speaker_id,
                       speaker_source=t.speaker.source, wave=t.wave,
                       shown_source_lang=t.shown_source_lang,
                       shown_source_text=t.shown_source_text,
                       candidate_text=t.shown_candidate_text, sent_at=_iso(t.sent_at),
                       replied_at=_iso(t.replied_at), stars=t.stars,
                       corrected_text=t.corrected_text, task_status=t.status,
                       share_consent=t.share_consent,
                       paid_kes=p.amount_kes if p else 0,
                       payout_status=p.status if p else None)
            out.append({k: row.get(k) for k in EXPORT_FIELDS})
    return out


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


# ------------------------------------------------------------ shared data
SHARED_FIELDS = ["record_id", "lang", "shown_source_lang", "shown_source_text",
                 "candidate_text", "candidate_origin", "model_ref", "stars",
                 "corrected_text", "item_outcome"]


class SharedDataSink(Protocol):
    def write(self, records: list[dict]) -> None: ...


class LocalJsonlSink:
    def __init__(self, path: str = "shared_data.jsonl"):
        self.path = Path(path)

    def write(self, records: list[dict]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


class HttpSink:
    def __init__(self, url: str, timeout: float = 30.0):
        self.url, self.http = url, httpx.Client(timeout=timeout)

    def write(self, records: list[dict]) -> None:
        self.http.post(self.url, json={"records": records}).raise_for_status()


def shared_record(t: ReviewTask, salt: str) -> dict:
    """Consented task -> anonymous record. record_id is stable (so the
    central service can drop duplicates) but reveals nothing."""
    rid = hmac.new(salt.encode(), f"task:{t.id}".encode(), hashlib.sha256).hexdigest()[:24]
    i = t.item
    return {"record_id": rid, "lang": i.lang, "shown_source_lang": t.shown_source_lang,
            "shown_source_text": t.shown_source_text, "candidate_text": t.shown_candidate_text,
            "candidate_origin": i.candidate.origin, "model_ref": i.candidate.model_ref,
            "stars": t.stars, "corrected_text": t.corrected_text, "item_outcome": i.outcome}


def pending_shared(db: Session, salt: str) -> tuple[list[int], list[dict]]:
    q = (select(ReviewTask).join(ReviewItem)
         .where(ReviewTask.share_consent.is_(True), ReviewTask.shared_at.is_(None),
                ReviewTask.replied_at.is_not(None), ReviewItem.state == "closed")
         .order_by(ReviewTask.id).limit(500))
    tasks = db.scalars(q).all()
    return [t.id for t in tasks], [shared_record(t, salt) for t in tasks]


def mark_shared(db: Session, ids: list[int], now: datetime) -> None:
    for t in db.scalars(select(ReviewTask).where(ReviewTask.id.in_(ids))):
        if t.share_consent:  # re-check: never mark/share a non-consented task
            t.shared_at = now


def export_shared(run: Callable, sink: SharedDataSink, salt: str,
                  clock: Callable[[], datetime]) -> int:
    """Collect under the lock, write outside it (HTTP can be slow), then mark."""
    ids, records = run(lambda db, eng: pending_shared(db, salt))
    if not records:
        return 0
    assert all(set(r) == set(SHARED_FIELDS) for r in records)
    sink.write(records)
    run(lambda db, eng: mark_shared(db, ids, clock()))
    return len(records)
