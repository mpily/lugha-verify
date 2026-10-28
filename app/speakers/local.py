"""SpeakerSource (CLAUDE.md §7). Local implementation: the speakers table,
network speakers seeded with source='network'."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ReviewTask, Speaker


class SpeakerSource(Protocol):
    def trusted(self, db: Session, lang: str) -> Speaker | None: ...
    def eligible(self, db: Session, lang: str, exclude_ids: set[int],
                 limit: int, min_accept_rate: float) -> list[Speaker]: ...


def _active(s: Speaker) -> bool:
    return s.opted_in_at is not None and s.opted_out_at is None


def _source_rank(s: Speaker) -> int:
    return 0 if s.source == "customer" else 1


class LocalSpeakerSource:
    def _last_tasked(self, db: Session) -> dict[int, datetime]:
        rows = db.execute(select(ReviewTask.speaker_id, func.max(ReviewTask.sent_at))
                          .group_by(ReviewTask.speaker_id)).all()
        return {sid: ts for sid, ts in rows}

    def _sorted(self, db: Session, speakers: list[Speaker]) -> list[Speaker]:
        last = self._last_tasked(db)
        never = datetime.min
        return sorted(speakers, key=lambda s: (
            _source_rank(s),
            -(s.accept_rate if s.accept_rate is not None else 1.0),
            last.get(s.id, never),
            s.id))

    def trusted(self, db: Session, lang: str) -> Speaker | None:
        cands = [s for s in db.scalars(select(Speaker)).all()
                 if _active(s) and lang in (s.trusted_for or [])]
        cands = self._sorted(db, cands)
        return cands[0] if cands else None

    def eligible(self, db: Session, lang: str, exclude_ids: set[int],
                 limit: int, min_accept_rate: float) -> list[Speaker]:
        out = []
        for s in db.scalars(select(Speaker)).all():
            if not _active(s) or s.id in exclude_ids or lang not in (s.languages or []):
                continue
            if s.accept_rate is not None and s.accept_rate < min_accept_rate:
                continue
            out.append(s)
        return self._sorted(db, out)[:limit]
