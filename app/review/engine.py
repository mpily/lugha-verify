"""Review state machine (CLAUDE.md §5).

One ReviewItem per (source text, language). States:
  new -> trusted (stage 1) -> fanout (stage 2) -> closed
Driven by tick() (scheduler, every 30 s) and handle_inbound() (SMS).
The engine never commits; the caller owns the transaction.

Rules decided with the user (status.md 2026-10-28):
- A fan-out task that passed its 30 min expiry is not paid and does not count;
  the reply is stored as 'late'. Speakers are told when a task expires.
- A reply with stars < 4 and no correction is stored as 'no_correction': not
  paid, not counted. The speaker may still send a correction on the same code.
  From the trusted speaker it sends the item to fan-out at once.
- The trusted speaker's late reply (after 15 min, item still open) counts as
  a stage-2 response (paid half), per CLAUDE.md §5.
- Payouts are recorded as 'pending'; the airtime client pays them (step 3).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (Candidate, InboundSms, Payout, ReviewItem, ReviewTask,
                      SourceText, Speaker, Translation)
from ..sms import templates as T
from ..sms.codes import new_code
from ..sms.parser import Keyword, ParseError, Reply, normalize_for_compare, parse_sms
from ..speakers.local import LocalSpeakerSource, SpeakerSource

OPEN_STATES = ("trusted", "fanout")
VALID_REPLY = ("replied", "accepted")


class Outbox(Protocol):
    def send(self, speaker: Speaker, text: str) -> None: ...


@dataclass
class Effective:
    text: str | None  # what this responder says the translation should be


class ReviewEngine:
    def __init__(self, db: Session, settings: Settings, clock: Callable[[], datetime],
                 outbox: Outbox, speakers: SpeakerSource | None = None,
                 rng: random.Random | None = None):
        self.db, self.s, self.clock, self.outbox = db, settings, clock, outbox
        self.speakers = speakers or LocalSpeakerSource()
        self.rng = rng or random.SystemRandom()

    # ---------------------------------------------------------------- helpers
    def _send(self, speaker: Speaker, text: str) -> None:
        if speaker.opted_out_at is None:  # never message an opted-out speaker
            self.outbox.send(speaker, text)

    def _open_codes(self) -> set[str]:
        q = (select(ReviewTask.code).join(ReviewItem)
             .where(ReviewItem.state.in_(OPEN_STATES),
                    ReviewTask.status.in_(("sent", "expired"))))
        return set(self.db.scalars(q).all())

    def _deadline(self, item: ReviewItem) -> datetime:
        return item.candidate.source.campaign.review_deadline

    def _create_task(self, item: ReviewItem, sp: Speaker, wave: int,
                     minutes: int) -> ReviewTask:
        now = self.clock()
        t = ReviewTask(item=item, speaker_id=sp.id, speaker=sp,
                       code=new_code(self._open_codes(), self.rng), wave=wave,
                       sent_at=now, expires_at=now + timedelta(minutes=minutes),
                       status="sent")
        self.db.add(t)
        self.db.flush()
        c = item.candidate
        self._send(sp, T.task_sms(t.code, item.lang, c.text, c.source.text))
        return t

    def _send_wave(self, item: ReviewItem) -> int:
        tasked = {t.speaker_id for t in item.tasks}
        wave = max((t.wave for t in item.tasks), default=0) + 1
        sps = self.speakers.eligible(self.db, item.lang, tasked,
                                     self.s.fanout_wave_size, self.s.min_accept_rate)
        for sp in sps:
            self._create_task(item, sp, wave, self.s.speaker_timeout_min)
        return len(sps)

    def _effective(self, item: ReviewItem, t: ReviewTask) -> str | None:
        if t.corrected_text:
            return t.corrected_text
        if item.candidate.text is not None and t.stars is not None and t.stars >= 4:
            return item.candidate.text
        return None

    def _valid_replies(self, item: ReviewItem) -> list[ReviewTask]:
        rs = [t for t in item.tasks if t.status in VALID_REPLY]
        return sorted(rs, key=lambda t: (t.replied_at, t.id))

    # ------------------------------------------------------------------ start
    def start_item(self, item: ReviewItem) -> None:
        assert item.state == "new"
        now = self.clock()
        item.started_at = now
        item.budget_kes = item.budget_kes or self.s.payout_per_item_kes
        mem = self._memory(item)
        if mem is not None:
            self._close(item, "verified", mem.final_text, [], notify=False,
                        n_reviews=mem.n_reviews)
            return
        trusted = self.speakers.trusted(self.db, item.lang)
        if trusted is not None:
            item.state = "trusted"
            self._create_task(item, trusted, 0, self.s.trusted_timeout_min)
        else:
            item.state = "fanout"
            self._send_wave(item)

    def _memory(self, item: ReviewItem) -> Translation | None:
        src = item.candidate.source
        q = (select(Translation).join(SourceText, Translation.source_id == SourceText.id)
             .where(Translation.lang == item.lang, Translation.status == "verified",
                    SourceText.text == src.text, SourceText.id != src.id))
        return self.db.scalars(q).first()

    # ------------------------------------------------------------------- tick
    def tick(self) -> None:
        items = self.db.scalars(select(ReviewItem)
                                .where(ReviewItem.state.in_(OPEN_STATES))).all()
        for item in items:
            self._advance(item)

    def _advance(self, item: ReviewItem) -> None:
        if item.state not in OPEN_STATES:
            return
        now = self.clock()
        if now >= self._deadline(item):
            self._close_at_deadline(item)
            return
        if item.state == "trusted":
            t = next(x for x in item.tasks if x.wave == 0)
            if now >= t.expires_at or t.speaker.opted_out_at is not None:
                if t.status == "sent":
                    t.status = "expired"
                item.state = "fanout"
                self._send_wave(item)
            return
        # fan-out: expire overdue tasks, then send the next wave if needed
        for t in item.tasks:
            if t.wave > 0 and t.status in ("sent", "no_correction") and now >= t.expires_at:
                t.status = "expired"
                self._send(t.speaker, T.expired_sms(t.code))
        wave = max(t.wave for t in item.tasks) if item.tasks else 0
        current = [t for t in item.tasks if t.wave == wave]
        done = all(t.status != "sent" or t.speaker.opted_out_at is not None
                   for t in current)
        if done and len(self._valid_replies(item)) < 2:
            self._send_wave(item)  # may send 0 when the pool is exhausted

    def _close_at_deadline(self, item: ReviewItem) -> None:
        replies = self._valid_replies(item)
        half = item.budget_kes // 2
        if replies:
            t = replies[0]
            self._close(item, "unverified", self._effective(item, t),
                        [(t, half)], notify=True, status="speaker_corrected")
        else:
            self._close(item, "no_reply", None, [], notify=True, status="machine")

    # ------------------------------------------------------------------ close
    def _close(self, item: ReviewItem, outcome: str, final_text: str | None,
               paid: list[tuple[ReviewTask, int]], notify: bool,
               status: str | None = None, n_reviews: int | None = None) -> None:
        now = self.clock()
        assert sum(a for _, a in paid) <= item.budget_kes, "budget exceeded"
        item.state, item.closed_at, item.outcome = "closed", now, outcome
        for t, amount in paid:
            t.status = "accepted"
            if self.db.scalar(select(Payout).where(Payout.review_task_id == t.id)) is None:
                self.db.add(Payout(review_task_id=t.id, speaker_id=t.speaker_id,
                                   amount_kes=amount, status="pending"))
            self._send(t.speaker, T.thanks_sms(t.code, amount))
        paid_ids = {t.id for t, _ in paid}
        for t in item.tasks:
            if t.id in paid_ids:
                continue
            waiting = t.status in ("sent", "no_correction") or (
                t.status == "expired" and t.wave == 0)  # trusted could still reply
            if not waiting:
                continue
            if t.status != "no_correction":
                t.status = "late" if outcome != "no_reply" else "expired"
            if notify:
                self._send(t.speaker, T.filled_sms(t.code) if outcome != "no_reply"
                           else T.closed_sms(t.code))
        status = status or ("verified" if outcome == "verified" else "speaker_corrected")
        src_id = item.candidate.source_id
        tr = self.db.scalar(select(Translation).where(Translation.source_id == src_id,
                                                      Translation.lang == item.lang))
        if tr is None:
            tr = Translation(source_id=src_id, lang=item.lang)
            self.db.add(tr)
        if status == "machine":
            final_text = item.candidate.text
        tr.final_text, tr.status = final_text, status
        tr.n_reviews = n_reviews if n_reviews is not None else len(self._valid_replies(item))
        self.db.flush()

    # ---------------------------------------------------------------- inbound
    def handle_inbound(self, phone_hash: str, text: str) -> None:
        now = self.clock()
        parsed = parse_sms(text)
        prev = self.db.scalars(select(InboundSms).where(InboundSms.from_hash == phone_hash)
                               .order_by(InboundSms.id.desc())).first()
        self.db.add(InboundSms(from_hash=phone_hash, text=text, received_at=now,
                               parsed_ok=not isinstance(parsed, ParseError),
                               error=parsed.reason if isinstance(parsed, ParseError) else None))
        sp = self.db.scalar(select(Speaker).where(Speaker.phone_hash == phone_hash))
        if sp is None:
            self.db.flush()
            return  # unknown number: logged, not answered
        if isinstance(parsed, Keyword):
            self._keyword(sp, parsed.word)
        elif isinstance(parsed, ParseError):
            if prev is None or prev.parsed_ok:  # one helpful reply, then ignore
                self._send(sp, T.parse_error_sms())
        else:
            self._reply(sp, parsed)
        self.db.flush()

    def _keyword(self, sp: Speaker, word: str) -> None:
        now = self.clock()
        if word == "STOP":
            self._send(sp, T.opted_out_sms())
            sp.opted_out_at = now
            open_tasks = self.db.scalars(
                select(ReviewTask).join(ReviewItem)
                .where(ReviewTask.speaker_id == sp.id, ReviewTask.status == "sent",
                       ReviewItem.state.in_(OPEN_STATES))).all()
            for t in open_tasks:
                t.status = "expired"
            for item in {t.item for t in open_tasks}:
                self._advance(item)
        elif word == "YES":
            sp.opted_in_at, sp.opted_out_at = now, None
            self._send(sp, T.opted_in_sms())
        else:
            self._send(sp, T.help_sms())

    def _reply(self, sp: Speaker, r: Reply) -> None:
        now = self.clock()
        if sp.opted_in_at is None or sp.opted_out_at is not None:
            self._send(sp, T.not_opted_in_sms())
            return
        t = self.db.scalars(select(ReviewTask)
                            .where(ReviewTask.code == r.code, ReviewTask.speaker_id == sp.id)
                            .order_by(ReviewTask.sent_at.desc(), ReviewTask.id.desc())).first()
        if t is None:  # unknown code, or someone else's: same answer
            self._send(sp, T.unknown_code_sms(r.code))
            return
        item = t.item
        self._advance(item)  # apply any timeout that is due before this reply
        if t.replied_at is not None and t.status != "no_correction":
            return  # duplicate reply / duplicate callback: first one stands
        if t.status == "expired" and t.speaker.opted_out_at is not None:
            return
        if item.candidate.text is None and not r.text:
            self._send(sp, T.parse_error_sms())
            return
        t.replied_at, t.stars, t.corrected_text, t.share_consent = now, r.stars, r.text, r.share
        if item.state == "closed":
            t.status = "late"
            self._send(sp, T.late_reply_sms(t.code))
            return
        if t.wave > 0 and (t.status == "expired" or now >= t.expires_at):
            t.status = "late"
            self._send(sp, T.expired_reply_sms(t.code))
            return
        n_words = len(item.candidate.source.text.split())
        if n_words > 5 and (now - t.sent_at).total_seconds() < self.s.min_reply_seconds:
            t.status = "invalid"
            self._send(sp, T.too_fast_sms(t.code))
            return
        if self._effective(item, t) is None:
            t.status = "no_correction"
            self._send(sp, T.no_correction_sms(t.code))
            if item.state == "trusted" and t.wave == 0:
                item.state = "fanout"
                self._send_wave(item)
            return
        t.status = "replied"
        if item.state == "trusted" and t.wave == 0:
            self._close(item, "verified", self._effective(item, t),
                        [(t, item.budget_kes)], notify=True)
            return
        replies = self._valid_replies(item)
        if len(replies) < 2:
            self._send(sp, T.thanks_pending_sms(t.code))
            return
        a, b = replies[:2]
        ea, eb = self._effective(item, a), self._effective(item, b)
        agree = normalize_for_compare(ea) == normalize_for_compare(eb)
        half = item.budget_kes // 2
        self._close(item, "verified" if agree else "unverified",
                    ea if agree else (a.corrected_text or b.corrected_text),
                    [(a, half), (b, half)], notify=True)
