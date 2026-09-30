"""Подключение к БД через SQLAlchemy ORM (SQLite по умолчанию, MSSQL — как в CurrentChatBot)."""
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from save_config import settings
from models import Base

logger = logging.getLogger(__name__)


def _database_url() -> str:
    return settings.database_url()


class Database:
    def __init__(self) -> None:
        self._engine = None
        self._session_factory = None

    def connect(self) -> None:
        url = _database_url()
        kwargs = {"echo": settings.SQL_ECHO, "pool_pre_ping": True}
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            # запросы выполняются в потоках (asyncio.to_thread)
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_recycle"] = 3600
            if url.startswith("mssql"):
                kwargs["fast_executemany"] = True
        self._engine = create_engine(url, **kwargs)

        if is_sqlite:
            @event.listens_for(self._engine, "connect")
            def _enable_fk(dbapi_conn, _record):    # без этого каскадные удаления не работают
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._session_factory = sessionmaker(bind=self._engine, autoflush=False, expire_on_commit=False)
        if settings.uses_mssql():
            # hide_password не маскирует PWD внутри odbc_connect — пишем без секретов
            logger.info("БД: mssql %s:%s/%s (user=%s, driver=%s)", settings.DB_HOST, settings.DB_PORT,
                        settings.DB_NAME, settings.DB_USER, settings.DB_DRIVER)
        else:
            logger.info("БД: %s", self._engine.url.render_as_string(hide_password=True))

    def create_all(self) -> None:
        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()


db = Database()
