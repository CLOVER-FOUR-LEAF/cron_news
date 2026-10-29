"""展示文本工具：把正文/摘要压成适合卡片展示的短摘要。"""

from __future__ import annotations

import re


def strip_markdown(text: str | None, limit: int = 2000) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*>\s?", "", value, flags=re.M)
    value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.M)
    value = re.sub(r"^\s*\d+[.)]\s+", "", value, flags=re.M)
    value = re.sub(r"[*_~`]{1,3}", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:limit].strip()


def first_sentences(text: str, limit: int = 140) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    if len(value) <= limit:
        return value
    window = value[: limit + 40]
    cutoff = -1
    for mark in ("。", "！", "？", ". ", "! ", "? ", "；", ";"):
        index = window.find(mark)
        if index >= 0 and (cutoff < 0 or index < cutoff):
            cutoff = index + len(mark)
    if 40 <= cutoff <= limit + 20:
        return value[:cutoff].strip()
    return value[:limit].rstrip("，,、；; ") + "…"


def short_summary(
    summary: str | None,
    content: str | None = None,
    limit: int = 140,
) -> str:
    """优先用摘要，摘要缺失或太短时从正文提取；始终压成短句。"""
    candidate = strip_markdown(summary, limit=limit * 4)
    if len(candidate) < 20:
        candidate = strip_markdown(content, limit=limit * 4)
    candidate = first_sentences(candidate, limit=limit)
    return candidate


def normalize_summary(
    summary: str | None,
    content: str | None = None,
    limit: int = 300,
) -> str:
    """入库时使用：保留比卡片略长的摘要，但不超过 limit，避免把全文塞进 summary。"""
    candidate = strip_markdown(summary, limit=limit * 3)
    if not candidate:
        candidate = strip_markdown(content, limit=limit * 3)
    candidate = first_sentences(candidate, limit=limit)
    return candidate.strip()
