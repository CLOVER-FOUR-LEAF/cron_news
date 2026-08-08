"""搜索服务厂商适配层。

不同搜索厂商的接口、鉴权方式、请求参数与响应结构各不相同，本模块为每个厂商提供独立适配器，
统一收敛为结构相同的 SearchHit（title / url / snippet / source / published_date），上层无需关心厂商差异。

支持厂商：
- tavily   官方 Tavily 搜索（POST /search，Bearer 鉴权）
- bocha    博查 AI 搜索（国内直连，POST /v1/web-search，Bearer 鉴权）
- searxng  自建元搜索（GET /search?format=json，一般无需鉴权）
- exa      Exa 语义搜索（POST /search，Bearer 鉴权）
- custom   自定义：兼容 /search 接口（POST，Bearer，请求体含 query/max_results，返回 {results:[...]}）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import httpx

AUTH_BEARER = "bearer"
AUTH_NONE = "none"


@dataclass
class SearchHit:
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    published_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SearchProvider:
    key: str = "custom"
    label: str = "自定义"
    default_base_url: str = ""
    auth: str = AUTH_BEARER
    hint: str = ""

    async def search(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        query: str,
        max_results: int,
        hours: int | None,
    ) -> list[SearchHit]:
        raise NotImplementedError


class TavilyProvider(SearchProvider):
    key = "tavily"
    label = "Tavily"
    default_base_url = "https://api.tavily.com"
    hint = "Tavily 官方搜索，需在 tavily.com 注册 API Key"

    async def search(self, client, base_url, api_key, query, max_results, hours):
        body: dict = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_answer": False,
        }
        if hours:
            days = max(1, min(int(round(hours / 24)), 365))
            body["days"] = days

        response = await client.post(
            f"{base_url}/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        _raise_for_status(response, self.label)
        data = response.json()
        hits = []
        for r in data.get("results", []) or []:
            hits.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or "").strip(),
                    snippet=(r.get("content") or r.get("snippet") or "").strip(),
                    source=(r.get("domain") or "").strip(),
                    published_date=(r.get("published_date") or "").strip(),
                )
            )
        return hits


class BochaProvider(SearchProvider):
    key = "bocha"
    label = "博查 Bocha"
    default_base_url = "https://api.bochaai.com"
    hint = "博查 AI 搜索（国内直连），key.bochaai.com 获取 API Key，单次请求上限 10 条"

    @staticmethod
    def _freshness(hours: int | None) -> str:
        if not hours or hours <= 0:
            return "noLimit"
        if hours <= 24:
            return "oneDay"
        if hours <= 168:
            return "oneWeek"
        if hours <= 720:
            return "oneMonth"
        return "oneYear"

    async def search(self, client, base_url, api_key, query, max_results, hours):
        body = {
            "query": query,
            "count": max(1, min(max_results, 10)),
            "freshness": self._freshness(hours),
        }
        response = await client.post(
            f"{base_url}/v1/web-search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        _raise_for_status(response, self.label)
        data = response.json()
        hits = []
        web_pages = (data.get("data") or {}).get("webPages") or {}
        for r in web_pages.get("value", []) or []:
            hits.append(
                SearchHit(
                    title=(r.get("name") or "").strip(),
                    url=(r.get("url") or "").strip(),
                    snippet=(r.get("snippet") or r.get("summary") or "").strip(),
                    source=(r.get("siteName") or "").strip(),
                    published_date=(r.get("datePublished") or "").strip(),
                )
            )
        return hits


class SearXNGProvider(SearchProvider):
    key = "searxng"
    label = "SearXNG"
    default_base_url = "http://localhost:8080"
    auth = AUTH_NONE
    hint = "自建元搜索服务，填入 SearXNG 实例地址（如 http://host:8080），一般无需 API Key"

    async def search(self, client, base_url, api_key, query, max_results, hours):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = await client.get(
            f"{base_url}/search",
            params={"q": query, "format": "json"},
            headers=headers,
        )
        _raise_for_status(response, self.label)
        data = response.json()
        hits = []
        for r in (data.get("results", []) or [])[:max_results]:
            hits.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or "").strip(),
                    snippet=(r.get("content") or "").strip(),
                    source=(r.get("engine") or "").strip(),
                    published_date=(r.get("publishedDate") or "").strip(),
                )
            )
        return hits


class ExaProvider(SearchProvider):
    key = "exa"
    label = "Exa"
    default_base_url = "https://api.exa.ai"
    hint = "Exa 语义搜索，exa.ai 获取 API Key"

    async def search(self, client, base_url, api_key, query, max_results, hours):
        body: dict = {"query": query, "numResults": max_results, "type": "auto"}
        if hours:
            body["startPublishedDate"] = (datetime.now() - timedelta(hours=hours)).isoformat()
        response = await client.post(
            f"{base_url}/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        _raise_for_status(response, self.label)
        data = response.json()
        hits = []
        for r in data.get("results", []) or []:
            hits.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or "").strip(),
                    snippet=(r.get("text") or r.get("highlights") or "").strip(),
                    source=(r.get("author") or r.get("domain") or "").strip(),
                    published_date=(r.get("publishedDate") or "").strip(),
                )
            )
        return hits


class GenericSearchProvider(SearchProvider):
    key = "custom"
    label = "自定义"
    default_base_url = ""
    hint = "兼容 POST {base_url}/search 接口的服务：Bearer 鉴权，请求体含 query/max_results，返回 {results:[{title,url,...}]}"

    async def search(self, client, base_url, api_key, query, max_results, hours):
        body: dict = {"query": query, "max_results": max_results}
        if hours:
            now = datetime.now()
            body["hours"] = hours
            body["from"] = (now - timedelta(hours=hours)).isoformat()
            body["to"] = now.isoformat()
        response = await client.post(
            f"{base_url}/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        _raise_for_status(response, self.label)
        data = response.json()
        hits = []
        for r in data.get("results", []) or []:
            hits.append(
                SearchHit(
                    title=(r.get("title") or "").strip(),
                    url=(r.get("url") or r.get("link") or "").strip(),
                    snippet=(r.get("snippet") or r.get("content") or r.get("description") or "").strip(),
                    source=(r.get("source") or r.get("domain") or "").strip(),
                    published_date=(r.get("published_date") or r.get("date") or "").strip(),
                )
            )
        return hits


_PROVIDERS: list[SearchProvider] = [
    TavilyProvider(),
    BochaProvider(),
    SearXNGProvider(),
    ExaProvider(),
    GenericSearchProvider(),
]

_PROVIDER_MAP = {p.key: p for p in _PROVIDERS}

# 厂商关键词 → 适配器 key（支持中英文服务商名称；exa 用词边界避免误匹配 example.com 等域名）
_KEYWORDS = (
    ("tavily", "tavily"),
    ("bocha", "bocha"),
    ("博查", "bocha"),
    ("searxng", "searxng"),
    (r"searx(?!\w)", "searxng"),
    (r"\bexa\b", "exa"),
)


def _key_from_text(text: str) -> str:
    import re

    lowered = (text or "").strip().lower()
    for keyword, key in _KEYWORDS:
        if re.search(keyword, lowered):
            return key
    return ""


def resolve_provider(provider: str, base_url: str) -> SearchProvider:
    """根据服务商名称 / Base URL 解析出对应适配器，无法识别时回退到自定义。"""
    key = _key_from_text(provider)
    if not key and base_url:
        key = _key_from_text(base_url)
    return _PROVIDER_MAP.get(key or "custom", _PROVIDER_MAP["custom"])


def provider_options() -> list[dict]:
    return [
        {
            "key": p.key,
            "label": p.label,
            "default_base_url": p.default_base_url,
            "auth": p.auth,
            "hint": p.hint,
        }
        for p in _PROVIDERS
    ]


def _raise_for_status(response: httpx.Response, label: str):
    if response.status_code < 400:
        return
    try:
        data = response.json()
        msg = (
            (data.get("error") or {}).get("message")
            or data.get("message")
            or data.get("detail")
            or response.text[:200]
        )
    except Exception:
        msg = response.text[:200]
    raise ValueError(f"{label} 请求失败（{response.status_code}）：{msg}")


async def run_search(
    provider: str,
    base_url: str,
    api_key: str,
    query: str,
    max_results: int = 10,
    hours: int | None = None,
) -> list[dict]:
    """按厂商适配器执行搜索，返回统一结构的字典列表。"""
    adapter = resolve_provider(provider, base_url)
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        base_url = adapter.default_base_url
    if not base_url:
        raise ValueError("搜索服务 Base URL 未填写")

    if adapter.auth == AUTH_BEARER and not api_key:
        raise ValueError(f"{adapter.label} 需要 API Key")

    headers = {"Content-Type": "application/json"}
    if adapter.auth == AUTH_BEARER:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=40.0) as client:
        hits = await adapter.search(client, base_url, api_key, query, max_results, hours)
    return [h.to_dict() for h in hits]
