"""旧库补列：PostgreSQL 没有 DATETIME 类型。"""

from types import SimpleNamespace

from app.database import _timestamp_ddl_type


def test_timestamp_type_by_dialect():
    postgres = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    sqlite = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    mysql = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    assert _timestamp_ddl_type(postgres) == "TIMESTAMP"
    assert _timestamp_ddl_type(sqlite) == "DATETIME"
    assert _timestamp_ddl_type(mysql) == "DATETIME"
