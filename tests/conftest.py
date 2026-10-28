from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.db import Base, make_session_factory
from app.models import Campaign, Candidate, ReviewItem, SourceText, Speaker
from app.phones import encrypt_phone, hash_phone
from app.review.engine import ReviewEngine

T0 = datetime(2026, 10, 29, 9, 0, 0)
KEY = Fernet.generate_key().decode()
SALT = "test-salt"


def make_settings(**kw) -> Settings:
    base = dict(at_username="sandbox", at_api_key="k", at_shortcode="12345",
                phone_enc_key=KEY, phone_hash_salt=SALT, admin_token="t",
                database_url="sqlite://")
    base.update(kw)
    return Settings(_env_file=None, **base)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Outbox:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send(self, speaker, text):
        self.sent.append((speaker.id, text))

    def to(self, sid):
        return [t for s, t in self.sent if s == sid]


class World:
    def __init__(self, **settings_kw):
        self.settings = make_settings(**settings_kw)
        engine, factory = make_session_factory("sqlite://")
        Base.metadata.create_all(engine)
        self.db = factory()
        self.clock = Clock()
        self.outbox = Outbox()
        self.engine = ReviewEngine(self.db, self.settings, self.clock, self.outbox,
                                   rng=random.Random(0))
        self._n = 0

    def speaker(self, langs=("dav",), trusted=(), source="customer", opted_in=True,
                accept=0, reject=0) -> Speaker:
        self._n += 1
        phone = f"+2547000000{self._n:02d}"
        sp = Speaker(phone_hash=hash_phone(phone, SALT), phone_enc=encrypt_phone(phone, KEY),
                     languages=list(langs), trusted_for=list(trusted), source=source,
                     opted_in_at=T0 - timedelta(days=1) if opted_in else None,
                     accept_count=accept, reject_count=reject)
        self.db.add(sp)
        self.db.flush()
        return sp

    def item(self, source="Wash your hands before eating food today please",
             candidate="Choka yacha", lang="dav", deadline_min=120) -> ReviewItem:
        c = Campaign(mode="bulk", source_lang="eng", fallback_lang="swh", created_at=T0,
                     review_deadline=self.clock.now + timedelta(minutes=deadline_min),
                     status="running")
        st = SourceText(campaign=c, text=source, swahili_text=None)
        cand = Candidate(source=st, lang=lang, text=candidate, origin="uploaded")
        it = ReviewItem(candidate=cand, lang=lang, budget_kes=self.settings.payout_per_item_kes)
        self.db.add_all([c, st, cand, it])
        self.db.flush()
        return it

    def code_for(self, item, sp):
        return next(t.code for t in item.tasks if t.speaker_id == sp.id)

    def reply(self, sp, text):
        self.engine.handle_inbound(sp.phone_hash, text)


@pytest.fixture
def w():
    return World()
