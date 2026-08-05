from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = None
async_session = None


class Base(DeclarativeBase):
    pass


def _build_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_async_engine(url, echo=settings.DEBUG, connect_args=connect_args)


def activate_engine(url: str):
    global engine, async_session
    engine = _build_engine(url)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


def get_session():
    return async_session()


activate_engine(settings.DATABASE_URL)


async def get_db():
    async with get_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_category_color_column)
        await conn.run_sync(_ensure_news_related_ids_column)
        await conn.run_sync(_ensure_news_read_at_column)
        await conn.run_sync(_ensure_news_user_action_columns)


def _ensure_category_color_column(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "categories" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("categories")]
        if "color" not in cols:
            sync_conn.execute(text("ALTER TABLE categories ADD COLUMN color VARCHAR(20)"))


def _ensure_news_related_ids_column(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "news" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("news")]
        if "related_ids" not in cols:
            sync_conn.execute(text("ALTER TABLE news ADD COLUMN related_ids TEXT"))


def _ensure_news_read_at_column(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "news" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("news")]
        if "read_at" not in cols:
            sync_conn.execute(text("ALTER TABLE news ADD COLUMN read_at DATETIME"))


def _ensure_news_user_action_columns(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "news" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("news")]
    additions = [
        ("is_fav", "ALTER TABLE news ADD COLUMN is_fav SMALLINT DEFAULT 0"),
        ("fav_at", "ALTER TABLE news ADD COLUMN fav_at DATETIME"),
        ("is_later", "ALTER TABLE news ADD COLUMN is_later SMALLINT DEFAULT 0"),
        ("later_at", "ALTER TABLE news ADD COLUMN later_at DATETIME"),
        ("is_reading", "ALTER TABLE news ADD COLUMN is_reading SMALLINT DEFAULT 0"),
    ]
    for col, ddl in additions:
        if col not in cols:
            sync_conn.execute(text(ddl))
