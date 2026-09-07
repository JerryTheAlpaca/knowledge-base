"""SQLite 引擎与会话。WAL、foreign_keys=ON、busy_timeout=5000、synchronous=FULL（docs/02 §7.1）。"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=FULL")
    cur.close()


def make_engine(settings: Settings | None = None):
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(get_engine())
    return _session_factory


def get_db():
    """FastAPI 依赖：每请求一个短事务会话。"""
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
