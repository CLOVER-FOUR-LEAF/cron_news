from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = None
async_session = None


class Base(DeclarativeBase):
    pass


def _build_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, echo=settings.DEBUG, connect_args=connect_args)

    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                # WAL + busy_timeout：允许聊天写入与采集任务并发，避免 database is locked
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    return engine


def activate_engine(url: str):
    global engine, async_session
    engine = _build_engine(url)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 数据库方言可能变了（SQLite <-> PostgreSQL），向量后端必须跟着重建，
    # 否则 pgvector / BLOB 会继续沿用旧引擎的会话工厂
    try:
        from app.services.vector_store import reset_vector_store

        reset_vector_store()
    except Exception:
        pass
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
        await conn.run_sync(ensure_schema)


def ensure_schema(sync_conn):
    """统一的建表/补列/索引入口，启动与切库共用。"""
    import app.models  # noqa: F401  确保所有模型已注册到 Base.metadata

    Base.metadata.create_all(sync_conn)
    _ensure_category_color_column(sync_conn)
    _ensure_news_related_ids_column(sync_conn)
    _ensure_news_read_at_column(sync_conn)
    _ensure_news_user_action_columns(sync_conn)
    _ensure_news_quality_columns(sync_conn)
    _ensure_news_batch_column(sync_conn)
    _ensure_brief_columns(sync_conn)
    _ensure_model_config_api_key_column(sync_conn)
    _ensure_model_config_native_search_columns(sync_conn)

    from app.services.fts import ensure_fts

    ensure_fts(sync_conn)


async def commit_with_retry(session: AsyncSession, attempts: int = 3) -> None:
    """SQLite 写锁重试：短暂等待后重新提交，避免聊天与采集并发时直接失败。"""
    import asyncio

    for attempt in range(attempts):
        try:
            await session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= attempts - 1:
                await session.rollback()
                raise
            await session.rollback()
            await asyncio.sleep(0.2 * (attempt + 1))


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
        ("reading_at", "ALTER TABLE news ADD COLUMN reading_at DATETIME"),
    ]
    for col, ddl in additions:
        if col not in cols:
            sync_conn.execute(text(ddl))


def _ensure_news_quality_columns(sync_conn):
    """采集质量字段：标签 / 可信度 / 结构化分析，旧库平滑补列。"""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "news" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("news")]
    additions = [
        ("tags", "ALTER TABLE news ADD COLUMN tags TEXT"),
        ("credibility", "ALTER TABLE news ADD COLUMN credibility INTEGER"),
        ("analysis", "ALTER TABLE news ADD COLUMN analysis TEXT"),
    ]
    for col, ddl in additions:
        if col not in cols:
            sync_conn.execute(text(ddl))


def _ensure_news_batch_column(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "news" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("news")]
    if "batch_id" not in cols:
        sync_conn.execute(text("ALTER TABLE news ADD COLUMN batch_id VARCHAR(40)"))


def _ensure_brief_columns(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    tables = inspector.get_table_names()

    if "briefs" not in tables:
        return

    cols = [c["name"] for c in inspector.get_columns("briefs")]
    if "note" not in cols:
        sync_conn.execute(text("ALTER TABLE briefs ADD COLUMN note TEXT DEFAULT ''"))
    if "source" not in cols:
        sync_conn.execute(text("ALTER TABLE briefs ADD COLUMN source VARCHAR(10) DEFAULT '自主'"))
    if "updated_at" not in cols:
        sync_conn.execute(text("ALTER TABLE briefs ADD COLUMN updated_at DATETIME"))

    # 将旧版独立便签表（brief_notes）合并进 briefs 后删除
    if "brief_notes" in tables:
        rows = sync_conn.execute(
            text("SELECT category_name, brief_date, content FROM brief_notes")
        ).fetchall()
        for category_name, brief_date, content in rows:
            exists = sync_conn.execute(
                text("SELECT 1 FROM briefs WHERE category_name = :c AND brief_date = :d"),
                {"c": category_name, "d": brief_date},
            ).scalar()
            if exists:
                sync_conn.execute(
                    text("UPDATE briefs SET note = :n WHERE category_name = :c AND brief_date = :d"),
                    {"n": content or "", "c": category_name, "d": brief_date},
                )
            else:
                sync_conn.execute(
                    text(
                        "INSERT INTO briefs (category_name, brief_date, content, note, source) "
                        "VALUES (:c, :d, '', :n, '外部')"
                    ),
                    {"c": category_name, "d": brief_date, "n": content or ""},
                )
        sync_conn.execute(text("DROP TABLE brief_notes"))


def _ensure_model_config_api_key_column(sync_conn):
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "model_configs" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("model_configs")]
    if "api_key_enc" not in cols:
        sync_conn.execute(text("ALTER TABLE model_configs ADD COLUMN api_key_enc TEXT"))


def _timestamp_ddl_type(sync_conn) -> str:
    """PostgreSQL 没有 DATETIME 类型；SQLite / MySQL / MariaDB 使用 DATETIME。"""
    return "TIMESTAMP" if sync_conn.dialect.name == "postgresql" else "DATETIME"


def _ensure_model_config_native_search_columns(sync_conn):
    """大模型内置联网搜索能力：旧库平滑补列。"""
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    if "model_configs" not in inspector.get_table_names():
        return
    cols = [c["name"] for c in inspector.get_columns("model_configs")]
    timestamp = _timestamp_ddl_type(sync_conn)
    additions = [
        ("native_search", "ALTER TABLE model_configs ADD COLUMN native_search BOOLEAN DEFAULT FALSE"),
        ("native_search_kind", "ALTER TABLE model_configs ADD COLUMN native_search_kind VARCHAR(30) DEFAULT ''"),
        ("native_search_detail", "ALTER TABLE model_configs ADD COLUMN native_search_detail VARCHAR(300) DEFAULT ''"),
        ("native_search_checked_at", f"ALTER TABLE model_configs ADD COLUMN native_search_checked_at {timestamp}"),
    ]
    for col, ddl in additions:
        if col not in cols:
            sync_conn.execute(text(ddl))
