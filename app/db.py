from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base


def make_engine(url: str):
    if url.startswith("sqlite"):
        database = make_url(url).database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            # SQLAlchemy controls explicit transactions. SQLite ignores FOR
            # UPDATE, so mutations must reserve the write lock before reading.
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection):
            statement = (
                "BEGIN IMMEDIATE" if connection.get_execution_options().get("sqlite_write") else "BEGIN"
            )
            connection.exec_driver_sql(statement)

        return engine
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def begin_write(session: Session) -> None:
    """Call before the first query of an application write transaction.

    PostgreSQL uses row locks in callers; the small SQLite demo serializes writes
    before reading state, avoiding stale-object writes across cleanup/restoring.
    """
    if session.get_bind().dialect.name == "sqlite":
        if session.in_transaction():
            raise RuntimeError("begin_write must precede queries in a write transaction")
        session.connection(execution_options={"sqlite_write": True})


@contextmanager
def write_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        begin_write(session)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
