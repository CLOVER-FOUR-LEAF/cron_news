"""原文抓取与正文抽取。

采集 Agent 不再只依赖搜索摘要：尽量抓取原文，提取正文、标题与发布时间，
供结构化分析、去重和可信度判断使用。抓取失败不影响主流程，会回退到搜索摘要。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.logging_setup import get_logger

logger = get_logger("article_fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CronNewsBot/1.0"
)
MAX_BYTES = 2_000_000
MAX_TEXT_CHARS = 14000
MIN_TEXT_CHARS = 80

BOILERPLATE = (
    "登录", "注册", "扫码", "下载客户端", "广告", "版权所有", "免责声明",
    "责任编辑", "本文来自", "关注我们", "点击查看", "更多精彩", "免责",
    "Copyright", "All Rights Reserved", "Privacy Policy", "Terms of Service",
)
CONTENT_SELECTORS = (
    "article",
    "main",
    "[role='main']",
    ".article-content",
    ".article-body",
    ".post-content",
    ".entry-content",
    ".content",
    "#content",
    ".main-content",
)


def _private_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass
    return False


async def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if _private_host(parsed.hostname):
        return False
    # 域名解析到内网地址（SSRF 防护）
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(loop.getaddrinfo(parsed.hostname, None), timeout=3.0)
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except (socket.gaierror, OSError, asyncio.TimeoutError):
        return False
    return True


def _clean_line(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^[•·\-•]\s*", "", text)
    return text


def _useful(line: str) -> bool:
    if len(line) < 12:
        return False
    return not any(token.lower() in line.lower() for token in BOILERPLATE)


def _meta(soup: BeautifulSoup, *, prop: str | None = None, name: str | None = None) -> str:
    attrs = {}
    if prop:
        attrs["property"] = prop
    if name:
        attrs["name"] = name
    if not attrs:
        return ""
    tag = soup.find("meta", attrs=attrs)
    return (tag.get("content") or "").strip() if tag else ""


def _published_date(soup: BeautifulSoup) -> str:
    for value in (
        _meta(soup, prop="article:published_time"),
        _meta(soup, prop="og:published_time"),
        _meta(soup, name="pubdate"),
        _meta(soup, name="publishdate"),
        _meta(soup, name="date"),
    ):
        if value:
            return value
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        return str(time_tag.get("datetime")).strip()
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("datePublished") or item.get("dateCreated")
            if value:
                return str(value)
    return ""


def _candidate_node(soup: BeautifulSoup):
    best = None
    best_score = 0
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector)[:3]:
            paragraphs = node.find_all(["p", "h2", "h3", "li", "blockquote"])
            text_len = sum(len(_clean_line(p.get_text(" ", strip=True))) for p in paragraphs)
            score = text_len + len(paragraphs) * 20
            if score > best_score:
                best, best_score = node, score
    if best is not None and best_score > 200:
        return best
    return soup.body or soup


def extract_article_text(html: str, *, base_url: str = "") -> dict[str, Any]:
    """从 HTML 中抽取正文。返回 {ok,text,title,published_date,chars,reason}。"""
    if not html:
        return {"ok": False, "reason": "empty_html", "text": "", "title": "", "chars": 0}
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside"]):
        tag.decompose()

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = _clean_line(h1.get_text(" ", strip=True))
    if not title:
        title = _clean_line((soup.title.string if soup.title and soup.title.string else ""))
    published = _published_date(soup)

    node = _candidate_node(soup)
    blocks: list[str] = []
    seen: set[str] = set()
    for element in node.find_all(["h2", "h3", "p", "li", "blockquote"]):
        line = _clean_line(element.get_text(" ", strip=True))
        if not _useful(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        if element.name in {"h2", "h3"}:
            blocks.append(f"## {line}")
        elif element.name == "li":
            blocks.append(f"- {line}")
        elif element.name == "blockquote":
            blocks.append(f"> {line}")
        else:
            blocks.append(line)

    text = "\n\n".join(blocks).strip()
    if len(text) < MIN_TEXT_CHARS:
        # 有些站点正文全在 div 里，没有 p 标签，退化为整段文本
        fallback = _clean_line(node.get_text(" ", strip=True))
        if len(fallback) > len(text):
            text = fallback

    text = text[:MAX_TEXT_CHARS]
    return {
        "ok": len(text) >= MIN_TEXT_CHARS,
        "text": text,
        "title": title[:255],
        "published_date": published,
        "chars": len(text),
        "reason": "" if len(text) >= MIN_TEXT_CHARS else "too_short",
    }


async def fetch_original(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """抓取并抽取原文；任何异常都转成 ok=False 的结果。"""
    if not url:
        return {"ok": False, "reason": "no_url", "text": "", "chars": 0}
    if not await is_public_url(url):
        return {"ok": False, "reason": "blocked_url", "text": "", "chars": 0}

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return {
                        "ok": False,
                        "reason": f"http_{response.status_code}",
                        "text": "",
                        "chars": 0,
                    }
                if not await is_public_url(str(response.url)):
                    return {"ok": False, "reason": "blocked_redirect", "text": "", "chars": 0}
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and "html" not in content_type and "text/plain" not in content_type:
                    return {"ok": False, "reason": "not_html", "text": "", "chars": 0}
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
                charset = "utf-8"
                match = re.search(r"charset=([\w\-]+)", content_type)
                if match:
                    charset = match.group(1).strip("\"'")
    except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
        logger.debug("原文抓取失败 %s：%s", url, exc)
        return {"ok": False, "reason": f"{type(exc).__name__}", "text": "", "chars": 0}

    html = None
    for encoding in (charset, "utf-8", "gb18030"):
        try:
            html = raw.decode(encoding, errors="strict")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if html is None:
        html = raw.decode("utf-8", errors="replace")

    result = extract_article_text(html, base_url=url)
    result["url"] = url
    return result


async def fetch_many(
    urls: list[str],
    *,
    concurrency: int = 4,
    timeout: float = 15.0,
) -> dict[str, dict[str, Any]]:
    """并发抓取多个 URL，返回 url -> result。"""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(url: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            return url, await fetch_original(url, timeout=timeout)

    pairs = await asyncio.gather(*(one(url) for url in urls), return_exceptions=True)
    results: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if isinstance(pair, Exception):
            continue
        url, result = pair
        results[url] = result
    return results


def best_fetch_url(hit: dict[str, Any]) -> str:
    return str(hit.get("url") or hit.get("link") or "").strip()
