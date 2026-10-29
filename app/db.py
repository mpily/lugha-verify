from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def normalize_db_url(url: str) -> str:
    """Hosting platforms hand out `postgres://` or `postgresql://`; use the
    psycopg 3 driver for both."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_session_factory(url: str):
    url = normalize_db_url(url)
    kw = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool  # one shared in-memory database
    else:
        kw["pool_pre_ping"] = True  # survive database restarts / idle drops
    engine = create_engine(url, **kw)
    return engine, sessionmaker(engine, expire_on_commit=False)
