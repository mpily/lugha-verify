"""The Alembic migrations must build exactly the schema the models describe."""
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.db import Base, normalize_db_url
from tests.conftest import reset_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def urls(tmp_path):
    out = [f"sqlite:///{tmp_path}/m.db"]
    if os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"):
        out.append(os.environ["TEST_DATABASE_URL"])
    return out


def test_upgrade_matches_models(tmp_path):
    for url in urls(tmp_path):
        if url.startswith("postgres"):
            reset_db()
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.attributes["url"] = url
        command.upgrade(cfg, "head")
        eng = create_engine(normalize_db_url(url), poolclass=NullPool)
        with eng.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(ctx, Base.metadata) == [], url
        command.downgrade(cfg, "base")
        eng.dispose()


def test_app_refuses_unmigrated_database():
    if not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"):
        pytest.skip("Postgres only")
    from app.main import create_app
    from tests.conftest import TEST_DB, make_settings
    s = make_settings()  # reset + migrated
    eng = create_engine(normalize_db_url(TEST_DB), poolclass=NullPool)
    with eng.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")
    with pytest.raises(SystemExit, match="alembic upgrade head"):
        create_app(s, start_scheduler=False)
