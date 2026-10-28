"""Data model (CLAUDE.md §4). Arrays are JSON columns so SQLite and Postgres
both work."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Speaker(Base):
    __tablename__ = "speakers"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone_enc: Mapped[str] = mapped_column(Text)
    # Languages the speaker is comfortable with, most preferred first. A
    # speaker reviews into any of them and is shown the source in the first
    # one available (e.g. ["eng","swh","dav"]).
    languages: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(16), default="customer")  # customer|network
    trusted_for: Mapped[list] = mapped_column(JSON, default=list)
    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    accept_count: Mapped[int] = mapped_column(Integer, default=0)
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    airtime_paid_total: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def accept_rate(self) -> float | None:
        n = self.accept_count + self.reject_count
        return self.accept_count / n if n else None


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String(64), index=True)
    phone_enc: Mapped[str] = mapped_column(Text)
    preferred_lang: Mapped[str] = mapped_column(String(8))
    list_id: Mapped[str] = mapped_column(String(64), index=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))  # bulk|research
    source_lang: Mapped[str] = mapped_column(String(8))
    fallback_lang: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    review_deadline: Mapped[datetime] = mapped_column(DateTime)
    # draft -> running -> delivered (bulk) | done (research)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    list_id: Mapped[str | None] = mapped_column(String(64))  # bulk mode


class SourceText(Base):
    __tablename__ = "source_texts"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    swahili_text: Mapped[str | None] = mapped_column(Text)
    campaign: Mapped[Campaign] = relationship()


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_texts.id"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    text: Mapped[str | None] = mapped_column(Text)  # None = no candidate
    origin: Mapped[str] = mapped_column(String(16))  # default_api|customer_model|uploaded
    model_ref: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[SourceText] = relationship()


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    # state: new -> trusted -> fanout -> closed
    state: Mapped[str] = mapped_column(String(16), default="new")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    budget_kes: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(16))  # verified|unverified|no_reply
    # Set when reviewed Swahili differs from the machine Swahili the candidate
    # was made from: re-translate from this text before tasking more speakers.
    retranslate_from: Mapped[str | None] = mapped_column(Text)
    candidate: Mapped[Candidate] = relationship()
    tasks: Mapped[list["ReviewTask"]] = relationship(back_populates="item")


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    review_item_id: Mapped[int] = mapped_column(ForeignKey("review_items.id"), index=True)
    speaker_id: Mapped[int] = mapped_column(ForeignKey("speakers.id"), index=True)
    code: Mapped[str] = mapped_column(String(4), index=True)
    wave: Mapped[int] = mapped_column(Integer)  # 0 = trusted stage
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    stars: Mapped[int | None] = mapped_column(Integer)
    corrected_text: Mapped[str | None] = mapped_column(Text)
    share_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    # sent|replied|accepted|late|expired|invalid
    status: Mapped[str] = mapped_column(String(16), default="sent")
    is_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    shown_source_lang: Mapped[str | None] = mapped_column(String(8))
    shown_source_text: Mapped[str | None] = mapped_column(Text)
    # The candidate this speaker rated (it can change after re-translation).
    shown_candidate_text: Mapped[str | None] = mapped_column(Text)
    item: Mapped[ReviewItem] = relationship(back_populates="tasks")
    speaker: Mapped[Speaker] = relationship()


class Translation(Base):
    __tablename__ = "translations"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_texts.id"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    final_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))  # machine|speaker_corrected|verified
    n_reviews: Mapped[int] = mapped_column(Integer, default=0)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    # unique: a task can never be paid twice
    review_task_id: Mapped[int] = mapped_column(ForeignKey("review_tasks.id"), unique=True)
    speaker_id: Mapped[int] = mapped_column(ForeignKey("speakers.id"), index=True)
    amount_kes: Mapped[int] = mapped_column(Integer)
    at_transaction_id: Mapped[str | None] = mapped_column(String(100), index=True)
    # pending -> sending -> sent -> success | failed;  unknown = AT may or may
    # not have paid (ambiguous network error): never retried automatically.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("campaign_id", "recipient_id"),)  # once each
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("recipients.id"))
    lang_sent: Mapped[str] = mapped_column(String(8))
    text_sent: Mapped[str] = mapped_column(Text)
    was_fallback: Mapped[bool] = mapped_column(Boolean)
    at_message_id: Mapped[str | None] = mapped_column(String(100))
    segments: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[str | None] = mapped_column(String(32))
    delivery_status: Mapped[str | None] = mapped_column(String(32))


class InboundSms(Base):
    __tablename__ = "inbound_sms"
    id: Mapped[int] = mapped_column(primary_key=True)
    at_id: Mapped[str | None] = mapped_column(String(100), unique=True)  # AT message id
    from_hash: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    parsed_ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(String(64))


class OutboundSms(Base):
    """Every SMS we send goes through this queue: the engine writes rows inside
    its transaction, a sender flushes them to AT after commit."""
    __tablename__ = "outbound_sms"
    id: Mapped[int] = mapped_column(primary_key=True)
    speaker_id: Mapped[int | None] = mapped_column(ForeignKey("speakers.id"), index=True)
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("deliveries.id"), index=True)
    phone_enc: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # queued|sending|sent|failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    at_message_id: Mapped[str | None] = mapped_column(String(100), index=True)
    at_status: Mapped[str | None] = mapped_column(String(64))
    cost: Mapped[str | None] = mapped_column(String(32))
    segments: Mapped[int | None] = mapped_column(Integer)
    delivery_status: Mapped[str | None] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(String(64))
