"""Alembic environment: DATABASE_URL from the environment, models' metadata."""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app import models  # noqa: F401  (registers tables)
from app.db import Base, normalize_db_url

target_metadata = Base.metadata


def url() -> str:
    u = context.config.attributes.get("url") or os.environ.get("DATABASE_URL")
    if not u:
        raise SystemExit("DATABASE_URL is not set")
    return normalize_db_url(u)


def run_offline() -> None:
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(url(), poolclass=NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
