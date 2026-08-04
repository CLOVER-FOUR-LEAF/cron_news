import os
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import select, func, text

from app.config import settings
from app.database import Base, activate_engine
from app.env_store import read_env_file, write_env_file
from app.models import Category, News  # noqa: F401  确保模型注册到 Base.metadata

DB_TYPES = {
    "mysql": {"label": "MySQL", "port": 3306, "scheme": "mysql+aiomysql"},
    "mariadb": {"label": "MariaDB", "port": 3306, "scheme": "mysql+aiomysql"},
    "postgresql": {"label": "PostgreSQL", "port": 5432, "scheme": "postgresql+asyncpg"},
}

MIGRATION = {"state": "idle", "message": "", "detail": ""}


def get_db_state() -> dict:
    env = read_env_file()
    config = {
        "db_type": env.get("DB_TYPE", "mysql"),
        "host": env.get("DB_HOST", ""),
        "port": env.get("DB_PORT", ""),
        "name": env.get("DB_NAME", ""),
        "user": env.get("DB_USER", ""),
        "password": env.get("DB_PASSWORD", ""),
    }
    complete = (
        config["db_type"] in DB_TYPES
        and bool(config["host"].strip())
        and bool(config["port"].strip())
        and bool(config["name"].strip())
        and bool(config["user"].strip())
        and bool(config["password"].strip())
    )
    return {
        "mode": env.get("DB_MODE", "system"),
        "config": config,
        "complete": complete,
    }


def save_config(config: dict):
    env = read_env_file()
    env["DB_TYPE"] = config.get("db_type", "mysql")
    env["DB_HOST"] = config.get("host", "")
    env["DB_PORT"] = str(config.get("port", ""))
    env["DB_NAME"] = config.get("name", "")
    env["DB_USER"] = config.get("user", "")
    env["DB_PASSWORD"] = config.get("password", "")
    write_env_file(env)


def build_url(config: dict) -> str:
    meta = DB_TYPES[config["db_type"]]
    user = quote_plus(config["user"])
    password = quote_plus(config["password"])
    host = config["host"]
    port = int(config["port"])
    name = quote_plus(config["name"])
    return f"{meta['scheme']}://{user}:{password}@{host}:{port}/{name}"


def _admin_engine(config: dict):
    from sqlalchemy.ext.asyncio import create_async_engine

    meta = DB_TYPES[config["db_type"]]
    user = quote_plus(config["user"])
    password = quote_plus(config["password"])
    admin_db = "postgres" if config["db_type"] == "postgresql" else ""
    url = f"{meta['scheme']}://{user}:{password}@{config['host']}:{int(config['port'])}/{admin_db}"
    return create_async_engine(url, connect_args={} if not url.startswith("sqlite") else {"check_same_thread": False})


async def ensure_database_exists(config: dict):
    admin = _admin_engine(config)
    try:
        async with admin.begin() as conn:
            if config["db_type"] == "postgresql":
                row = await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": config["name"]},
                )
                if row.scalar() is None:
                    safe_name = config["name"].replace('"', '""')
                    await conn.execute(text(f'CREATE DATABASE "{safe_name}"'))
            else:
                safe_name = config["name"].replace("`", "``")
                await conn.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{safe_name}` "
                        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
    finally:
        await admin.dispose()


async def test_connection(config: dict) -> tuple[bool, str]:
    from sqlalchemy.ext.asyncio import create_async_engine

    if config["db_type"] not in DB_TYPES:
        return False, "不支持的数据库类型"
    try:
        await ensure_database_exists(config)
        engine = create_async_engine(build_url(config))
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True, "连接成功"
    except Exception as e:
        return False, f"连接失败：{e}"


def is_busy() -> bool:
    return MIGRATION["state"] == "migrating"


TABLE_ORDER = ["categories", "news"]


async def _copy_all(source_engine, target_engine) -> dict[str, int]:
    from app.database import (
        _ensure_category_color_column,
        _ensure_news_related_ids_column,
        _ensure_news_read_at_column,
        _ensure_news_user_action_columns,
    )

    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_category_color_column)
        await conn.run_sync(_ensure_news_related_ids_column)
        await conn.run_sync(_ensure_news_read_at_column)
        await conn.run_sync(_ensure_news_user_action_columns)

    counts = {}
    for table_name in TABLE_ORDER:
        table = Base.metadata.tables[table_name]
        async with source_engine.connect() as src:
            rows = (await src.execute(select(table))).mappings().all()
        data = [dict(r) for r in rows]
        async with target_engine.begin() as tgt:
            await tgt.execute(table.delete())
            if data:
                await tgt.execute(table.insert(), data)
        counts[table_name] = len(data)

    async with source_engine.connect() as src:
        for table_name, count in counts.items():
            table = Base.metadata.tables[table_name]
            src_count = (await src.execute(select(func.count()).select_from(table))).scalar()
            if src_count != count:
                raise RuntimeError(f"迁移校验失败：{table_name} 源库 {src_count} 条，目标库 {count} 条")
    return counts


async def run_switch(mode: str, keep_data: bool) -> dict:
    MIGRATION.update(state="migrating", message="正在迁移数据，请勿操作…", detail="")
    old_engine = None
    try:
        state = get_db_state()

        if mode == "standalone":
            if not state["complete"]:
                raise RuntimeError("独立数据库配置不完整")
            ok, msg = await test_connection(state["config"])
            if not ok:
                raise RuntimeError(msg)
            target_url = build_url(state["config"])
        else:
            target_url = settings.DATABASE_URL

        from app.database import engine as source_engine
        from sqlalchemy.ext.asyncio import create_async_engine

        target_engine = create_async_engine(
            target_url,
            connect_args={"check_same_thread": False} if target_url.startswith("sqlite") else {},
        )

        try:
            counts = await _copy_all(source_engine, target_engine)
        except Exception:
            await target_engine.dispose()
            raise

        old_engine = source_engine
        activate_engine(target_url)

        env = read_env_file()
        env["DB_MODE"] = mode
        write_env_file(env)

        detail = "、".join(f"{k} {v} 条" for k, v in counts.items())

        if not keep_data:
            if mode == "standalone":
                await old_engine.dispose()
                db_file = Path(settings.DB_PATH)
                if db_file.exists():
                    os.remove(db_file)
            else:
                async with old_engine.begin() as conn:
                    await conn.run_sync(Base.metadata.drop_all)
                await old_engine.dispose()

        MIGRATION.update(state="done", message="迁移完成", detail=detail)
        return MIGRATION
    except Exception as e:
        MIGRATION.update(state="error", message="迁移失败，已回滚，原数据库未受影响", detail=str(e))
        return MIGRATION
