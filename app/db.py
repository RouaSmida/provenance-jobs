"""Engine, session factory and schema creation."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# The events table is insert-only, and the database enforces it, not just the app code:
# any UPDATE or DELETE raises an error. A superuser can still bypass triggers
# (scripts/tamper.py does that on purpose); the hash chain is what detects it.
INSERT_ONLY_DDL = [
    """
    CREATE OR REPLACE FUNCTION events_insert_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'events is insert-only, % is not allowed', TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS events_insert_only ON events",
    """
    CREATE TRIGGER events_insert_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_insert_only()
    """,
]


def init_db() -> None:
    """Create the tables and the insert-only trigger. Safe to run again; the API runs it once at startup.

    There is no Alembic yet, and create_all only creates tables that are missing.
    """
    from app import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in INSERT_ONLY_DDL:
            conn.execute(text(stmt))
