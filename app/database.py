from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False},
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
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
