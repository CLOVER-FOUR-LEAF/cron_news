"""大模型厂商自带联网搜索的适配层。

部分厂商的模型/账号本身就能联网：
- DeepSeek  POST {root}/v1/tools/search
- Kimi      POST {root}/v1/tools/search
- 智谱 GLM  POST {root}/api/paas/v4/web_search
- MiMo      POST {base}/chat/completions，tools=[{"type":"web_search"}]，引用在 message.annotations

当用户没有配置独立搜索服务时，平台会自动回退到当前大模型的内置搜索（如果检测通过），
因此「大模型自带联网」的账号不必再单独买一个搜索 API。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from app.services.provider_presets import find_llm_preset
from app.services.search_providers import SearchHit

MAX_ERROR_CHARS = 240


@dataclass(frozen=True)
class NativeSearchSpec:
    kind: str
    label: str
    mode: str  # standalone=独立搜索接口；chat_tool=通过对话接口的联网工具
    provider_key: str


_SPECS = {
    "deepseek": NativeSearchSpec("deepseek", "DeepSeek 搜索", "standalone", "deepseek"),
    "kimi": NativeSearchSpec("kimi", "Kimi 搜索", "standalone", "kimi"),
    "zhipu": NativeSearchSpec("zhipu", "智谱 Web Search", "standalone", "zhipu"),
    "mimo": NativeSearchSpec("mimo", "MiMo 联网搜索", "chat_tool", "mimo"),
}


def resolve_spec(provider: str = "", base_url: str = "", model_id: str = "") -> NativeSearchSpec | None:
    """根据服务商名称或 Base URL 识别内置搜索能力；未知厂商返回 None。"""
    preset = find_llm_preset(provider, base_url, model_id)
    if not preset:
        return None
    native = preset.get("native_search") or {}
    return _SPECS.get(native.get("kind", ""))


def capability_available(
    provider: str,
    base_url: str,
    model_id: str,
    stored_available: bool,
    stored_checked_at: datetime | None,
) -> bool:
    """判断内置搜索是否可用。

    - 已经实际探测过：以探测结果为准（即使之后临时网络失败也不擅自改回可用）；
    - 从未探测：独立搜索接口按厂商能力乐观放行，对话工具型（MiMo）必须探测通过。
    """
    spec = resolve_spec(provider, base_url, model_id)
    if not spec:
        return False
    if stored_checked_at is not None:
        return bool(stored_available)
    return spec.mode == "standalone"


def _strip_suffix(url: str, *suffixes: str) -> str:
    value = (url or "").strip().rstrip("/")
    for suffix in suffixes:
        if value.endswith(suffix):
            return value[: -len(suffix)].rstrip("/")
    return value


def _base_url(base_url: str) -> str:
    return _strip_suffix(base_url, "/chat/completions", "/embeddings", "/completions")


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _freshness_zhipu(hours: int | None) -> str:
    if not hours or hours <= 0:
        return "noLimit"
    if hours <= 24:
        return "oneDay"
    if hours <= 168:
        return "oneWeek"
    if hours <= 720:
        return "oneMonth"
    return "oneYear"


def _after_date(hours: int | None) -> str:
    if not hours or hours <= 0:
        return ""
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d")


def _parse_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _filter_by_hours(hits: list[SearchHit], hours: int | None) -> list[SearchHit]:
    """按发布时间过滤；只有可解析时间占明显多数时才过滤，且保留无时间的结果。"""
    if not hours or hours <= 0 or not hits:
        return hits
    parsed = [(hit, _parse_date(hit.published_date)) for hit in hits]
    dated = [(hit, dt) for hit, dt in parsed if dt is not None]
    # 只有严格多数结果带可解析时间时才过滤，避免厂商少返回时间字段就误杀结果
    if len(dated) * 2 <= len(hits):
        return hits
    cutoff = datetime.now() - timedelta(hours=hours * 1.5)
    kept = [hit for hit, dt in parsed if dt is None or dt >= cutoff]
    return kept or hits


def parse_deepseek(payload: dict) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=url,
                snippet=str(item.get("snippet") or item.get("summary") or "").strip(),
                source=str(item.get("site_name") or _domain(url)),
                published_date=str(item.get("date") or "").strip(),
            )
        )
    return hits


def parse_kimi(payload: dict) -> list[SearchHit]:
    rows = payload.get("search_results") or payload.get("results") or []
    hits: list[SearchHit] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "").strip()
        if not url:
            continue
        snippet = item.get("snippet") or item.get("summary") or ""
        if not snippet:
            chunks = item.get("chunks") or []
            if isinstance(chunks, list):
                snippet = " ".join(
                    str(chunk.get("text") or "") for chunk in chunks if isinstance(chunk, dict)
                )
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=url,
                snippet=str(snippet).strip(),
                source=str(item.get("site_name") or _domain(url)),
                published_date=str(item.get("date") or "").strip(),
            )
        )
    return hits


def parse_zhipu(payload: dict) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in payload.get("search_result") or payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or item.get("url") or "").strip()
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=url,
                snippet=str(item.get("content") or item.get("snippet") or "").strip(),
                source=str(item.get("media") or item.get("site_name") or _domain(url)),
                published_date=str(item.get("publish_date") or item.get("date") or "").strip(),
            )
        )
    return hits


def parse_mimo(payload: dict) -> list[SearchHit]:
    choices = payload.get("choices") or []
    message = (choices[0] or {}).get("message") if choices else {}
    annotations = (message or {}).get("annotations") or payload.get("annotations") or []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in annotations:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=url,
                snippet=str(item.get("summary") or item.get("content") or "").strip(),
                source=str(item.get("site_name") or _domain(url)),
                published_date=str(item.get("publish_time") or item.get("published_date") or "").strip(),
            )
        )
    return hits


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:MAX_ERROR_CHARS]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)[:MAX_ERROR_CHARS]
        return str(data.get("message") or data.get("detail") or data)[:MAX_ERROR_CHARS]
    return str(data)[:MAX_ERROR_CHARS]


async def _post_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.status_code >= 400:
        raise ValueError(f"请求失败（{response.status_code}）：{_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError("返回了非 JSON 响应") from exc
    if not isinstance(data, dict):
        raise ValueError("返回了非预期 JSON 结构")
    return data


async def _search_deepseek(
    base_url: str, api_key: str, model_id: str, query: str, max_results: int, hours: int | None, timeout: float
) -> list[SearchHit]:
    root = _strip_suffix(_base_url(base_url), "/v1")
    text = query
    after = _after_date(hours)
    if after:
        text = f"{text} after:{after}"
    payload = await _post_json(
        f"{root}/v1/tools/search",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {"query": text[:128], "count": max(1, min(max_results, 50)), "need_content": False},
        timeout,
    )
    return parse_deepseek(payload)


async def _search_kimi(
    base_url: str, api_key: str, model_id: str, query: str, max_results: int, hours: int | None, timeout: float
) -> list[SearchHit]:
    root = _strip_suffix(_base_url(base_url), "/v1")
    payload = await _post_json(
        f"{root}/v1/tools/search",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "text_query": query,
            "limit": max(1, min(max_results, 20)),
            "timeout_seconds": max(1, min(int(timeout), 60)),
            "include_content": False,
        },
        timeout + 5,
    )
    return _filter_by_hours(parse_kimi(payload), hours)


async def _search_zhipu(
    base_url: str, api_key: str, model_id: str, query: str, max_results: int, hours: int | None, timeout: float
) -> list[SearchHit]:
    root = _strip_suffix(_base_url(base_url), "/api/paas/v4")
    payload = await _post_json(
        f"{root}/api/paas/v4/web_search",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "search_query": query[:70],
            "search_engine": "search_std",
            "search_intent": False,
            "count": max(1, min(max_results, 50)),
            "search_recency_filter": _freshness_zhipu(hours),
            "content_size": "medium",
        },
        timeout,
    )
    return _filter_by_hours(parse_zhipu(payload), hours)


async def _search_mimo(
    base_url: str, api_key: str, model_id: str, query: str, max_results: int, hours: int | None, timeout: float
) -> list[SearchHit]:
    base = _base_url(base_url)
    if not model_id:
        raise ValueError("MiMo 内置搜索需要填写模型 ID")
    text = query
    if hours:
        text = f"{query}（请优先检索最近 {hours} 小时内的信息）"
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "你是资讯检索助手。请联网检索并返回真实可访问的来源，不要编造链接。"},
            {"role": "user", "content": text},
        ],
        "tools": [
            {"type": "web_search", "max_keyword": 3, "force_search": True, "limit": 1}
        ],
        "tool_choice": "auto",
        "max_completion_tokens": 512,
        "temperature": 1.0,
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        payload = await _post_json(f"{base}/chat/completions", headers, body, timeout)
    except ValueError as exc:
        # 仅当错误与这两个非标准字段有关时才退化重试，避免无效 Key 被请求两次
        detail = str(exc).lower()
        if not any(token in detail for token in ("thinking", "max_completion", "max_tokens")):
            raise
        body.pop("thinking", None)
        body.pop("max_completion_tokens", None)
        body["max_tokens"] = 512
        payload = await _post_json(f"{base}/chat/completions", headers, body, timeout)
    return _filter_by_hours(parse_mimo(payload), hours)


_IMPLEMENTATIONS = {
    "deepseek": _search_deepseek,
    "kimi": _search_kimi,
    "zhipu": _search_zhipu,
    "mimo": _search_mimo,
}


async def run_native_search(
    provider: str,
    base_url: str,
    api_key: str,
    model_id: str,
    query: str,
    max_results: int = 10,
    hours: int | None = None,
    timeout: float = 45.0,
) -> list[dict]:
    """使用大模型厂商自带的搜索能力执行查询，返回与独立搜索服务一致的字典结构。"""
    spec = resolve_spec(provider, base_url, model_id)
    if not spec:
        raise ValueError("当前大模型未声明内置联网搜索能力")
    if not api_key:
        raise ValueError(f"使用 {spec.label} 需要大模型 API Key")
    impl = _IMPLEMENTATIONS.get(spec.kind)
    if not impl:
        raise ValueError(f"{spec.label} 暂未实现")
    hits = await impl(base_url, api_key, model_id, query, max_results, hours, timeout)
    return [hit.to_dict() for hit in hits[:max_results]]


async def detect_native_search(
    provider: str,
    base_url: str,
    api_key: str,
    model_id: str = "",
    timeout: float = 15.0,
) -> dict:
    """实际探测一次内置搜索，供「测试连接」与保存后校验使用。"""
    spec = resolve_spec(provider, base_url, model_id)
    if not spec:
        return {
            "available": False,
            "kind": "",
            "label": "",
            "detail": "该服务商没有可自动检测的内置联网搜索",
        }
    if not api_key:
        return {
            "available": False,
            "kind": spec.kind,
            "label": spec.label,
            "detail": "未填写 API Key，无法检测内置联网搜索",
        }
    try:
        hits = await run_native_search(
            provider,
            base_url,
            api_key,
            model_id,
            "人工智能 最新进展",
            max_results=3,
            hours=None,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "available": False,
            "kind": spec.kind,
            "label": spec.label,
            "detail": f"{spec.label} 检测失败：{str(exc)[:MAX_ERROR_CHARS]}",
        }
    if not hits:
        if spec.mode == "chat_tool":
            detail = (
                f"{spec.label} 接口可用，但没有返回搜索引用。"
                "请在厂商控制台开通联网插件后重试。"
            )
        else:
            detail = (
                f"{spec.label} 搜索接口可访问，但没有返回结果。"
                "请检查账号是否开通搜索权限，或改为配置独立搜索服务。"
            )
        return {
            "available": False,
            "kind": spec.kind,
            "label": spec.label,
            "detail": detail,
        }
    return {
        "available": True,
        "kind": spec.kind,
        "label": spec.label,
        "detail": f"检测成功（{spec.label}，返回 {len(hits)} 条参考来源）",
    }
