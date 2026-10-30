"""服务商预设模板。

只提供「填表提示」与能力声明，不包含任何密钥。用户在设置页选择模板后仍需自行填写 API Key。
预设同时用于：
1. 前端「新增配置」弹窗的厂商模板与常用模型候选；
2. 后端识别服务商（例如判断大模型是否自带联网搜索）；
3. 定时任务/配置告警判断「没有独立搜索服务时能否回退到模型内置搜索」。

新增厂商时，只要在这里补一条记录即可；搜索适配器仍需在 search_providers.py 中实现。
"""

from __future__ import annotations

from urllib.parse import urlparse

LLM_PRESETS: list[dict] = [
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "aliases": ["deepseek", "深度求索"],
        "hosts": ["api.deepseek.com"],
        "native_search": {
            "kind": "deepseek",
            "label": "DeepSeek 搜索",
            "mode": "standalone",
        },
        "docs": "https://platform.deepseek.com/",
        "note": "OpenAI 兼容接口。官方 Search API 与模型共用同一 API Key，保存后会自动探测内置联网能力。",
    },
    {
        "key": "mimo",
        "label": "MiMo（小米）",
        "base_url": "https://api.xiaomimimo.com/v1",
        "models": ["mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"],
        "aliases": ["mimo", "小米", "xiaomi", "xiaomimimo"],
        "hosts": ["api.xiaomimimo.com"],
        "native_search": {
            "kind": "mimo",
            "label": "MiMo 联网搜索",
            "mode": "chat_tool",
        },
        "docs": "https://platform.mimo.xiaomi.com/",
        "note": "内置联网需先在 MiMo 控制台开通「联网插件」。保存后请点「测试连接」验证；验证通过后即使不配置搜索服务也能采集。",
    },
    {
        "key": "kimi",
        "label": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            "kimi-k3.1-pro",
            "kimi-k3.1-pro-highspeed",
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.6",
        ],
        "aliases": ["kimi", "moonshot", "月之暗面"],
        "hosts": ["api.moonshot.cn", "api.moonshot.ai"],
        "native_search": {
            "kind": "kimi",
            "label": "Kimi 搜索",
            "mode": "standalone",
        },
        "docs": "https://platform.kimi.com/",
        "note": "OpenAI 兼容接口。官方 /v1/tools/search 与模型共用 API Key，可作为独立搜索服务的替代。",
    },
    {
        "key": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5"],
        "aliases": ["zhipu", "glm", "智谱", "bigmodel"],
        "hosts": ["open.bigmodel.cn", "api.bigmodel.cn"],
        "native_search": {
            "kind": "zhipu",
            "label": "智谱 Web Search",
            "mode": "standalone",
        },
        "docs": "https://open.bigmodel.cn/",
        "note": "OpenAI 兼容接口。官方 /web_search 接口与模型共用 API Key，可作为独立搜索服务的替代。",
    },
    {
        "key": "bailian",
        "label": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus"],
        "aliases": ["bailian", "dashscope", "qwen", "百炼", "通义", "千问"],
        "hosts": ["dashscope.aliyuncs.com"],
        "native_search_note": "官方联网搜索依赖 DashScope / Responses 协议，本平台当前使用 OpenAI Chat Completions，请另配搜索服务。",
        "docs": "https://bailian.console.aliyun.com/",
        "note": "OpenAI 兼容模式。如需内置联网，建议直接配置下方任一独立搜索服务。",
    },
    {
        "key": "ark",
        "label": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-2-1-pro-260915"],
        "aliases": ["ark", "volces", "doubao", "方舟", "豆包", "火山"],
        "hosts": ["ark.cn-beijing.volces.com", "ark.cn-shanghai.volces.com"],
        "native_search_note": "官方联网搜索走 Responses API；本平台当前使用 Chat Completions，请另配搜索服务。",
        "docs": "https://console.volcengine.com/ark/",
        "note": "模型 ID 通常带有日期后缀，请以方舟控制台「推理接入点」中的 ID 为准。",
    },
    {
        "key": "minimax",
        "label": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "models": ["MiniMax-M3.1-Flash", "MiniMax-M3"],
        "aliases": ["minimax", "mini max"],
        "hosts": ["api.minimaxi.com", "api.minimax.io", "api.minimax.chat"],
        "docs": "https://platform.minimaxi.com/",
        "note": "模型 ID 大小写敏感。国际站可改用 https://api.minimax.io/v1。",
    },
    {
        "key": "hunyuan",
        "label": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hy3"],
        "aliases": ["hunyuan", "tencent", "混元", "腾讯"],
        "hosts": ["api.hunyuan.cloud.tencent.com", "tokenhub.tencentmaas.com"],
        "docs": "https://cloud.tencent.com/product/hunyuan",
        "note": "新账号可能已迁移到腾讯云 TokenHub，若返回 404 请改用控制台给出的 OpenAI 兼容地址。",
    },
    {
        "key": "opencodego",
        "label": "OpenCode Go",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "models": [
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "glm-5.3",
            "glm-5.3-flash",
            "kimi-k3.1-pro",
            "kimi-k3",
            "mimo-v2.6-pro",
            "mimo-v2.6-flash",
            "minimax-m3",
            "qwen3.8-max",
        ],
        "aliases": ["opencodego", "opencode go", "opencode", "ocgo"],
        "hosts": ["opencode.ai"],
        "docs": "https://opencode.ai/docs/",
        "note": (
            "协议路由较特殊：本平台只走 OpenAI Chat Completions，请选择 Chat 协议模型；"
            "Claude / Gemini 等仅支持 Anthropic / Google 协议的模型无法在此使用。"
            "旧地址 https://opencode.ai/zen/go/v1 也可尝试。"
        ),
    },
    {
        "key": "openrouter",
        "label": "OpenRouter（聚合）",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [],
        "aliases": ["openrouter"],
        "hosts": ["openrouter.ai"],
        "docs": "https://openrouter.ai/models",
        "note": "模型 ID 需带厂商前缀（如 deepseek/deepseek-v4-pro），以 openrouter.ai/models 为准。",
    },
    {
        "key": "custom",
        "label": "自定义 / OpenAI 兼容",
        "base_url": "",
        "models": [],
        "aliases": ["custom", "自定义", "openai兼容", "openai compatible"],
        "hosts": [],
        "docs": "",
        "note": "任何兼容 POST {base_url}/chat/completions 的服务都可以使用。",
    },
]

IMAGE_PRESETS: list[dict] = [
    {
        "key": "bailian-image",
        "label": "阿里百炼 文生图",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["wanx2.1-t2i-turbo", "wanx2.1-t2i-plus"],
        "aliases": ["bailian image", "百炼文生图"],
        "hosts": ["dashscope.aliyuncs.com"],
        "docs": "https://bailian.console.aliyun.com/",
        "note": "使用 OpenAI 兼容的 /images/generations 接口。",
    },
    {
        "key": "zhipu-image",
        "label": "智谱 CogView",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["cogview-4"],
        "aliases": ["cogview", "智谱文生图"],
        "hosts": ["open.bigmodel.cn"],
        "docs": "https://open.bigmodel.cn/",
        "note": "使用 OpenAI 兼容的 /images/generations 接口。",
    },
    {
        "key": "ark-image",
        "label": "火山方舟 文生图",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seedream-3-0-t2i"],
        "aliases": ["seedream", "方舟文生图"],
        "hosts": ["ark.cn-beijing.volces.com"],
        "docs": "https://console.volcengine.com/ark/",
        "note": "模型 ID 以方舟控制台为准。",
    },
    {
        "key": "openai-image",
        "label": "OpenAI 文生图",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-image-1"],
        "aliases": ["openai image", "dall-e"],
        "hosts": ["api.openai.com"],
        "docs": "https://platform.openai.com/",
        "note": "",
    },
    {
        "key": "custom",
        "label": "自定义 / OpenAI 兼容",
        "base_url": "",
        "models": [],
        "aliases": ["custom", "自定义"],
        "hosts": [],
        "docs": "",
        "note": "兼容 POST {base_url}/images/generations 的服务。",
    },
]

SEARCH_PRESETS: list[dict] = [
    {
        "key": "tavily",
        "label": "Tavily",
        "aliases": ["tavily"],
        "base_url": "https://api.tavily.com",
        "auth": "bearer",
        "hint": "Tavily 官方搜索，需在 tavily.com 注册 API Key",
        "key_url": "https://app.tavily.com/",
    },
    {
        "key": "bocha",
        "label": "博查 Bocha",
        "aliases": ["bocha", "博查"],
        "base_url": "https://api.bochaai.com",
        "auth": "bearer",
        "hint": "国内直连，key.bochaai.com 获取 API Key，单次请求上限 10 条",
        "key_url": "https://key.bochaai.com/",
    },
    {
        "key": "searxng",
        "label": "SearXNG",
        "aliases": ["searxng", "searx"],
        "base_url": "http://localhost:8080",
        "auth": "none",
        "hint": "自建元搜索服务，填入实例地址（如 http://host:8080），一般无需 API Key",
        "key_url": "https://docs.searxng.org/",
    },
    {
        "key": "exa",
        "label": "Exa",
        "aliases": ["exa"],
        "base_url": "https://api.exa.ai",
        "auth": "bearer",
        "hint": "Exa 语义搜索，exa.ai 获取 API Key",
        "key_url": "https://exa.ai/",
    },
    {
        "key": "brave",
        "label": "Brave Search",
        "aliases": ["brave", "brave search"],
        "base_url": "https://api.search.brave.com",
        "auth": "brave",
        "hint": "Brave 独立索引，api.search.brave.com 申请 Key，免费额度较充足",
        "key_url": "https://api.search.brave.com/app/keys",
    },
    {
        "key": "serper",
        "label": "Serper（Google）",
        "aliases": ["serper", "serper.dev", "serper google"],
        "base_url": "https://google.serper.dev",
        "auth": "x-api-key",
        "hint": "Serper.dev 提供的 Google 搜索结果，按次计费",
        "key_url": "https://serper.dev/",
    },
    {
        "key": "serpapi",
        "label": "SerpAPI（Google）",
        "aliases": ["serpapi", "serp api", "serpapi google"],
        "base_url": "https://serpapi.com",
        "auth": "query",
        "hint": "SerpAPI 的 Google 搜索接口，Key 放在查询参数中",
        "key_url": "https://serpapi.com/",
    },
    {
        "key": "jina",
        "label": "Jina Search",
        "aliases": ["jina", "jina search", "jina reader"],
        "base_url": "https://s.jina.ai",
        "auth": "bearer",
        "hint": "Jina Reader 搜索接口，返回标题、链接与正文摘要",
        "key_url": "https://jina.ai/",
    },
    {
        "key": "firecrawl",
        "label": "Firecrawl",
        "aliases": ["firecrawl"],
        "base_url": "https://api.firecrawl.dev",
        "auth": "bearer",
        "hint": "Firecrawl Search，可选同时抓取网页正文",
        "key_url": "https://firecrawl.dev/",
    },
    {
        "key": "custom",
        "label": "自定义",
        "base_url": "",
        "auth": "bearer",
        "hint": "兼容 POST {base_url}/search 接口：Bearer 鉴权，请求体含 query/max_results，返回 {results:[...]}",
        "key_url": "",
    },
]

_PRESET_GROUPS = {
    "llm": LLM_PRESETS,
    "image": IMAGE_PRESETS,
    "search": SEARCH_PRESETS,
}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _norm(text: str) -> str:
    import re

    return re.sub(r"[\s\-_()（）]+", "", (text or "").strip().lower())


def _matches_text(preset: dict, text: str) -> bool:
    target = _norm(text)
    if not target:
        return False
    candidates = [preset.get("key", ""), preset.get("label", ""), *preset.get("aliases", [])]
    return any(_norm(candidate) == target for candidate in candidates)


def _matches_host(preset: dict, url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    hosts = list(preset.get("hosts", []))
    base_host = _host(preset.get("base_url", ""))
    if base_host:
        hosts.append(base_host)
    for item in hosts:
        item = str(item).lower()
        if host == item or host.endswith("." + item):
            return True
    return False


def find_preset(config_type: str, provider: str = "", base_url: str = "", model_id: str = "") -> dict | None:
    """按服务商名称或 Base URL 匹配预设；匹配不到返回 None。"""
    presets = _PRESET_GROUPS.get(config_type, [])
    for preset in presets:
        if preset.get("key") == "custom":
            continue
        if _matches_text(preset, provider):
            return preset
    for preset in presets:
        if preset.get("key") == "custom":
            continue
        if _matches_host(preset, base_url):
            return preset
    return None


def find_llm_preset(provider: str = "", base_url: str = "", model_id: str = "") -> dict | None:
    return find_preset("llm", provider, base_url, model_id)


def find_search_preset(provider: str = "", base_url: str = "") -> dict | None:
    return find_preset("search", provider, base_url)


def _public(preset: dict) -> dict:
    return {
        "key": preset.get("key", ""),
        "label": preset.get("label", ""),
        "base_url": preset.get("base_url", ""),
        "models": list(preset.get("models", [])),
        "auth": preset.get("auth", ""),
        "hint": preset.get("hint", ""),
        "docs": preset.get("docs", ""),
        "key_url": preset.get("key_url", ""),
        "note": preset.get("note", ""),
        "native_search": preset.get("native_search"),
        "native_search_note": preset.get("native_search_note", ""),
    }


def preset_payload() -> dict[str, list[dict]]:
    return {key: [_public(item) for item in group] for key, group in _PRESET_GROUPS.items()}


def provider_key_for(config_type: str, provider: str, base_url: str = "") -> str:
    preset = find_preset(config_type, provider, base_url)
    return preset["key"] if preset else (provider or "").strip()
