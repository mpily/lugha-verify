from __future__ import annotations

import os
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
# Set TEST_DATABASE_URL=postgresql://... to run the suite against Postgres.
TEST_DB = os.environ.get("TEST_DATABASE_URL") or "sqlite://"


def reset_db() -> None:
    """Fresh empty schema for each test on Postgres (in-memory SQLite is
    fresh anyway). Ends other sessions first so leaked pools can't block it."""
    if TEST_DB.startswith("sqlite"):
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool
    from app.db import normalize_db_url
    eng = create_engine(normalize_db_url(TEST_DB), poolclass=NullPool,
                        isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    from alembic import command
    from alembic.config import Config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.attributes["url"] = TEST_DB
    command.upgrade(cfg, "head")  # Postgres schema comes from the migrations
KEY = Fernet.generate_key().decode()
SALT = "test-salt"


def make_settings(**kw) -> Settings:
    base = dict(at_username="sandbox", at_api_key="k", at_shortcode="12345",
                phone_enc_key=KEY, phone_hash_salt=SALT, admin_token="t",
                database_url=TEST_DB)
    base.update(kw)
    if base["database_url"] == TEST_DB:
        reset_db()
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
        engine, factory = make_session_factory(self.settings.database_url)
        if self.settings.database_url.startswith("sqlite"):
            Base.metadata.create_all(engine)
        self.db = factory()
        self.clock = Clock()
        self.outbox = Outbox()
        self.engine = ReviewEngine(self.db, self.settings, self.clock, self.outbox,
                                   rng=random.Random(0))
        self._n = 0

    def speaker(self, langs=("eng", "dav"), trusted=(), source="customer", opted_in=True,
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

    def source(self, text, candidates, swahili_text=None, source_lang="eng",
               deadline_min=120):
        from app.review.setup import CandidateIn, create_review_items
        c = Campaign(mode="bulk", source_lang=source_lang, fallback_lang="swh",
                     created_at=self.clock.now, status="running",
                     review_deadline=self.clock.now + timedelta(minutes=deadline_min))
        st = SourceText(campaign=c, text=text, swahili_text=swahili_text)
        self.db.add_all([c, st])
        items = create_review_items(self.db, self.settings, st,
                                    {k: CandidateIn(v, "default_api", "nllb")
                                     for k, v in candidates.items()})
        return {i.lang: i for i in items}

    def code_for(self, item, sp):
        return next(t.code for t in item.tasks if t.speaker_id == sp.id)

    def reply(self, sp, text):
        self.engine.handle_inbound(sp.phone_hash, text)


@pytest.fixture
def w():
    return World()
