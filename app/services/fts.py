"""SQLite FTS5 检索支持。

只索引 news.title / news.summary，避免正文 trigram 索引体积膨胀；正文仍由 get_news 按需读取。
FTS5 不可用、非 SQLite、或查询词过短（trigram 至少 3 字）时，由 retrieval 回退到 LIKE。
"""

from __future__ import annotations

import re

from sqlalchemy import text

from app.logging_setup import get_logger

logger = get_logger("fts")

_available: bool | None = None
_trigram: bool = False

TERM_SPLIT_RE = re.compile(r"[\s,，;；、|/]+")


def ensure_fts(sync_conn) -> bool:
    """建表/建触发器/首次回填。仅 SQLite 生效，幂等。"""
    global _available, _trigram

    if sync_conn.dialect.name != "sqlite":
        _available = False
        _trigram = False
        logger.info("FTS 未启用：当前数据库不是 SQLite（检索将走 LIKE）")
        return False

    existing = sync_conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='news_fts'")
    ).scalar()

    if not existing:
        created = False
        for tokenizer in ("trigram", "unicode61"):
            try:
                sync_conn.execute(
                    text(
                        "CREATE VIRTUAL TABLE news_fts USING fts5("
                        "title, summary, content='news', content_rowid='id', "
                        f"tokenize='{tokenizer}')"
                    )
                )
                _trigram = tokenizer == "trigram"
                created = True
                logger.info("已创建 FTS5 索引（tokenize=%s）", tokenizer)
                break
            except Exception as exc:
                logger.warning("创建 FTS5 索引失败（tokenize=%s）：%s", tokenizer, exc)

        if not created:
            _available = False
            _trigram = False
            logger.warning("FTS5 不可用，检索将回退到 LIKE")
            return False
    else:
        _trigram = "trigram" in (existing or "").lower()

    _ensure_triggers(sync_conn)
    _available = True

    if not existing:
        try:
            rebuild(sync_conn)
        except Exception as exc:
            logger.warning("FTS 首次回填失败：%s", exc)

    return True


def _ensure_triggers(sync_conn) -> None:
    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS news_fts_ai AFTER INSERT ON news BEGIN
            INSERT INTO news_fts(rowid, title, summary)
            VALUES (new.id, new.title, new.summary);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS news_fts_ad AFTER DELETE ON news BEGIN
            INSERT INTO news_fts(news_fts, rowid, title, summary)
            VALUES ('delete', old.id, old.title, old.summary);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS news_fts_au AFTER UPDATE OF title, summary ON news BEGIN
            INSERT INTO news_fts(news_fts, rowid, title, summary)
            VALUES ('delete', old.id, old.title, old.summary);
            INSERT INTO news_fts(rowid, title, summary)
            VALUES (new.id, new.title, new.summary);
        END
        """,
    ]
    for stmt in statements:
        try:
            sync_conn.execute(text(stmt))
        except Exception as exc:
            logger.warning("创建 FTS 触发器失败：%s", exc)


def rebuild(sync_conn) -> None:
    sync_conn.execute(text("INSERT INTO news_fts(news_fts) VALUES('rebuild')"))


def is_available() -> bool:
    return bool(_available)


def is_trigram() -> bool:
    return bool(_trigram)


def split_terms(query: str, max_terms: int = 8) -> list[str]:
    terms: list[str] = []
    for part in TERM_SPLIT_RE.split(query or ""):
        term = part.strip().strip('"').strip("'")
        if term and term not in terms:
            terms.append(term)
    return terms[:max_terms]


def can_use_fts(terms: list[str]) -> bool:
    """trigram 要求每个词至少 3 字；非 trigram 分词器对中文子串不可靠，统一走 LIKE。"""
    if not _available or not _trigram or not terms:
        return False
    return all(len(term) >= 3 for term in terms)


def build_match_query(terms: list[str]) -> str | None:
    if not can_use_fts(terms):
        return None
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
