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
        self.T = T.Templates(settings.at_keyword)

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

    # -------------------------------------------------------------- sources
    def _sources(self, item: ReviewItem) -> dict[str, str]:
        """Source texts a reviewer of this item may be shown, by language:
        the original, customer Swahili, or reviewed machine Swahili (pivot)."""
        st = item.candidate.source
        out = {st.campaign.source_lang: st.text}
        if "swh" not in out:
            sw = st.swahili_text or self._pivot_swahili(st.id)
            if sw:
                out["swh"] = sw
        out.pop(item.lang, None)
        return out

    def _pivot_swahili(self, source_id: int) -> str | None:
        """Machine Swahili is usable as a source once a reviewer accepted or
        corrected it: the verified text if any, else the first valid reply."""
        tr = self.db.scalar(select(Translation).where(
            Translation.source_id == source_id, Translation.lang == "swh",
            Translation.status == "verified"))
        if tr is not None and tr.final_text:
            return tr.final_text
        q = (select(ReviewItem).join(Candidate)
             .where(Candidate.source_id == source_id, ReviewItem.lang == "swh"))
        for sw_item in self.db.scalars(q).all():
            replies = self._valid_replies(sw_item)
            if replies:
                return self._effective(sw_item, replies[0])
        return None

    def _on_swahili_available(self, sw_item: ReviewItem) -> None:
        """Speakers who read Swahili but not the original become eligible.
        If the reviewer changed the Swahili, machine candidates made from the
        old Swahili are re-translated first (the wave goes out once that is
        done, see apply_retranslation); otherwise the wave goes out now."""
        reviewed = self._effective(sw_item, self._valid_replies(sw_item)[0])
        changed = normalize_for_compare(reviewed) != normalize_for_compare(
            sw_item.candidate.text or "")
        src_id = sw_item.candidate.source_id
        q = (select(ReviewItem).join(Candidate)
             .where(Candidate.source_id == src_id, ReviewItem.lang != "swh",
                    ReviewItem.state.in_(OPEN_STATES)))
        for item in self.db.scalars(q).all():
            if changed and item.candidate.origin == "default_api":
                item.retranslate_from = reviewed
            elif item.state == "fanout" and len(self._valid_replies(item)) < 2:
                self._send_wave(item)

    # ------------------------------------------------------- first drafts
    @staticmethod
    def draft_source(item: ReviewItem, supports) -> tuple[str, str] | None:
        """(src lang, text) to machine-translate this item from, if the
        translator supports it: customer Swahili first, else the original."""
        st = item.candidate.source
        options = []
        if st.swahili_text and item.lang != "swh":
            options.append(("swh", st.swahili_text))
        options.append((st.campaign.source_lang, st.text))
        return next(((l, t) for l, t in options if l != item.lang and supports(l, item.lang)),
                    None)

    def needs_draft(self, item: ReviewItem, supports) -> bool:
        c = item.candidate
        return (c.text is None and c.origin == "default_api"
                and self.draft_source(item, supports) is not None)

    def pending_drafts(self, supports) -> list[tuple[int, str, str, str]]:
        """(item id, src lang, text, target lang) waiting for a first draft."""
        out = []
        for i in self.db.scalars(select(ReviewItem).where(ReviewItem.state == "drafting")):
            src = self.draft_source(i, supports)
            out.append((i.id, *(src or (None, None)), i.lang))
        return out

    def apply_draft(self, item_id: int, text: str | None, model_ref: str | None) -> None:
        """Store the machine draft (None = no candidate: speakers translate
        from scratch) and start the review."""
        item = self.db.get(ReviewItem, item_id)
        if item is None or item.state != "drafting":
            return
        if text:
            item.candidate.text, item.candidate.model_ref = text, model_ref
        item.state = "new"
        if self.clock() >= self._deadline(item):
            self._close_at_deadline(item)
            return
        self.start_item(item)

    def pending_retranslations(self) -> list[tuple[int, str, str]]:
        """(item id, reviewed Swahili, target lang) still to re-translate."""
        q = select(ReviewItem).where(ReviewItem.retranslate_from.is_not(None),
                                     ReviewItem.state.in_(OPEN_STATES))
        return [(i.id, i.retranslate_from, i.lang) for i in self.db.scalars(q).all()]

    def apply_retranslation(self, item_id: int, swahili: str, text: str | None,
                            model_ref: str | None) -> None:
        """Swap in the candidate re-made from reviewed Swahili, then task the
        Swahili readers. If the translator gave nothing, keep the old
        candidate rather than invent one. Earlier tasks keep the candidate they
        showed (shown_candidate_text), so their ratings stay correct."""
        item = self.db.get(ReviewItem, item_id)
        if item is None or item.retranslate_from != swahili:
            return  # changed meanwhile; a newer job will handle it
        item.retranslate_from = None
        if item.state not in OPEN_STATES:
            return
        if text and normalize_for_compare(text) != normalize_for_compare(
                item.candidate.text or ""):
            old = item.candidate
            item.candidate = Candidate(source=old.source, lang=old.lang, text=text,
                                       origin="default_api", model_ref=model_ref)
            self.db.add(item.candidate)
            self.db.flush()
        if item.state == "fanout" and len(self._valid_replies(item)) < 2:
            self._send_wave(item)

    def _create_task(self, item: ReviewItem, sp: Speaker, wave: int,
                     minutes: int, sources: dict[str, str]) -> ReviewTask:
        now = self.clock()
        shown = next(x for x in sp.languages if x in sources)
        t = ReviewTask(item=item, speaker_id=sp.id, speaker=sp,
                       code=new_code(self._open_codes(), self.rng), wave=wave,
                       sent_at=now, expires_at=now + timedelta(minutes=minutes),
                       status="sent", shown_source_lang=shown,
                       shown_source_text=sources[shown],
                       shown_candidate_text=item.candidate.text,
                       shown_candidate_id=item.candidate.id)
        self.db.add(t)
        self.db.flush()
        self._send(sp, self.T.task_sms(t.code, item.lang, item.candidate.text,
                                  sources[shown], shown))
        return t

    def _busy(self, item: ReviewItem) -> set[int]:
        """Speakers already on this item, or with an open task on the same
        source text in another language (one open task per sentence)."""
        q = (select(ReviewTask.speaker_id).join(ReviewItem).join(Candidate)
             .where(Candidate.source_id == item.candidate.source_id,
                    ReviewItem.id != item.id,
                    ReviewTask.status.in_(("sent", "no_correction"))))
        return {t.speaker_id for t in item.tasks} | set(self.db.scalars(q).all())

    def start_items(self, items: list[ReviewItem]) -> None:
        """Start target-language items before machine-Swahili items, so the
        scarce multilingual speakers go to the target languages first."""
        for item in sorted(items, key=lambda i: (i.lang == "swh", i.id)):
            self.start_item(item)

    def _send_wave(self, item: ReviewItem) -> int:
        tasked = self._busy(item)
        wave = max((t.wave for t in item.tasks), default=0) + 1
        sources = self._sources(item)
        sps = self.speakers.eligible(self.db, item.lang, set(sources), tasked,
                                     self.s.fanout_wave_size, self.s.min_accept_rate)
        for sp in sps:
            self._create_task(item, sp, wave, self.s.speaker_timeout_min, sources)
        return len(sps)

    def _effective(self, item: ReviewItem, t: ReviewTask) -> str | None:
        if t.corrected_text:
            return t.corrected_text
        if t.shown_candidate_text is not None and t.stars is not None and t.stars >= 4:
            return t.shown_candidate_text
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
        sources = self._sources(item)
        trusted = self.speakers.trusted(self.db, item.lang, set(sources), self._busy(item))
        if trusted is not None:
            item.state = "trusted"
            self._create_task(item, trusted, 0, self.s.trusted_timeout_min, sources)
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
                self._send(t.speaker, self.T.expired_sms(t.code))
        wave = max(t.wave for t in item.tasks) if item.tasks else 0
        current = [t for t in item.tasks if t.wave == wave]
        done = all(t.status != "sent" or t.speaker.opted_out_at is not None
                   for t in current)
        if done and len(self._valid_replies(item)) < 2 and item.retranslate_from is None:
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
            self._send(t.speaker, self.T.thanks_sms(t.code, amount))
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
                self._send(t.speaker, self.T.filled_sms(t.code) if outcome != "no_reply"
                           else self.T.closed_sms(t.code))
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
    def handle_inbound(self, phone_hash: str, text: str, at_id: str | None = None) -> None:
        now = self.clock()
        if at_id and self.db.scalar(select(InboundSms).where(InboundSms.at_id == at_id)):
            return  # AT retried the callback: already handled
        parsed = parse_sms(text, self.s.at_keyword)
        prev = self.db.scalars(select(InboundSms).where(InboundSms.from_hash == phone_hash)
                               .order_by(InboundSms.id.desc())).first()
        self.db.add(InboundSms(at_id=at_id, from_hash=phone_hash, text=text, received_at=now,
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
                self._send(sp, self.T.parse_error_sms())
        else:
            self._reply(sp, parsed)
        self.db.flush()

    def _keyword(self, sp: Speaker, word: str) -> None:
        now = self.clock()
        if word == "STOP":
            self._send(sp, self.T.opted_out_sms())
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
            self._send(sp, self.T.opted_in_sms())
        else:
            self._send(sp, self.T.help_sms())

    def _reply(self, sp: Speaker, r: Reply) -> None:
        now = self.clock()
        if sp.opted_in_at is None or sp.opted_out_at is not None:
            self._send(sp, self.T.not_opted_in_sms())
            return
        t = self.db.scalars(select(ReviewTask)
                            .where(ReviewTask.code == r.code, ReviewTask.speaker_id == sp.id)
                            .order_by(ReviewTask.sent_at.desc(), ReviewTask.id.desc())).first()
        if t is None:  # unknown code, or someone else's: same answer
            self._send(sp, self.T.unknown_code_sms(r.code))
            return
        item = t.item
        self._advance(item)  # apply any timeout that is due before this reply
        if t.replied_at is not None and t.status != "no_correction":
            return  # duplicate reply / duplicate callback: first one stands
        if t.status == "expired" and t.speaker.opted_out_at is not None:
            return
        if t.shown_candidate_text is None and not r.text:
            self._send(sp, self.T.parse_error_sms())
            return
        t.replied_at, t.stars, t.corrected_text, t.share_consent = now, r.stars, r.text, r.share
        if item.state == "closed":
            t.status = "late"
            self._send(sp, self.T.late_reply_sms(t.code))
            return
        if t.wave > 0 and (t.status == "expired" or now >= t.expires_at):
            t.status = "late"
            self._send(sp, self.T.expired_reply_sms(t.code))
            return
        n_words = len(item.candidate.source.text.split())
        if n_words > 5 and (now - t.sent_at).total_seconds() < self.s.min_reply_seconds:
            t.status = "invalid"
            self._send(sp, self.T.too_fast_sms(t.code))
            return
        if self._effective(item, t) is None:
            t.status = "no_correction"
            self._send(sp, self.T.no_correction_sms(t.code))
            if item.state == "trusted" and t.wave == 0:
                item.state = "fanout"
                self._send_wave(item)
            return
        t.status = "replied"
        first_swahili = (item.lang == "swh" and len(self._valid_replies(item)) == 1
                         and not item.candidate.source.swahili_text)
        self._settle(item, sp, t)
        if first_swahili:
            self._on_swahili_available(item)

    def _settle(self, item: ReviewItem, sp: Speaker, t: ReviewTask) -> None:
        if item.state == "trusted" and t.wave == 0:
            self._close(item, "verified", self._effective(item, t),
                        [(t, item.budget_kes)], notify=True)
            return
        replies = self._valid_replies(item)
        if len(replies) < 2:
            self._send(sp, self.T.thanks_pending_sms(t.code))
            return
        a, b = replies[:2]
        ea, eb = self._effective(item, a), self._effective(item, b)
        agree = normalize_for_compare(ea) == normalize_for_compare(eb)
        half = item.budget_kes // 2
        self._close(item, "verified" if agree else "unverified",
                    ea if agree else (a.corrected_text or b.corrected_text),
                    [(a, half), (b, half)], notify=True)
