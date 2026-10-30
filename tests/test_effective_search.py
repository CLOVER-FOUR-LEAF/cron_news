"""搜索能力选择：独立搜索服务优先，缺失时回退大模型内置联网。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import model_configs as mc


def _cfg(**overrides):
    base = dict(
        id=1,
        provider="deepseek",
        provider_name="DeepSeek",
        name="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        model_id="deepseek-flash",
        config_type="llm",
        enabled=True,
        env_key="",
        api_key_enc="enc",
        native_search=False,
        native_search_kind="deepseek",
        native_search_detail="",
        native_search_checked_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_native_fallback_when_search_service_missing(monkeypatch):
    llm = _cfg()

    async def fake_enabled(db, config_type):
        return llm if config_type == "llm" else None

    monkeypatch.setattr(mc, "get_enabled_config", fake_enabled)
    monkeypatch.setattr(mc, "decrypt_api_key", lambda enc: "secret")

    endpoint = await mc.get_effective_search_endpoint(None)
    assert endpoint is not None
    assert endpoint["source"] == "native"
    assert endpoint["native_kind"] == "deepseek"
    assert endpoint["api_key"] == "secret"


@pytest.mark.asyncio
async def test_service_takes_priority_over_native(monkeypatch):
    llm = _cfg()
    search = SimpleNamespace(
        id=2,
        provider="tavily",
        provider_name="Tavily",
        name="Tavily",
        base_url="https://api.tavily.com",
        model_id="",
        config_type="search",
        enabled=True,
        env_key="",
        api_key_enc="enc",
        native_search=False,
        native_search_kind="",
        native_search_detail="",
        native_search_checked_at=None,
    )

    async def fake_enabled(db, config_type):
        return {"llm": llm, "search": search}.get(config_type)

    monkeypatch.setattr(mc, "get_enabled_config", fake_enabled)
    monkeypatch.setattr(mc, "decrypt_api_key", lambda enc: "secret")

    endpoint = await mc.get_effective_search_endpoint(None)
    assert endpoint["source"] == "service"
    assert endpoint["provider"] == "tavily"


@pytest.mark.asyncio
async def test_searxng_does_not_require_api_key(monkeypatch):
    search = SimpleNamespace(
        id=3,
        provider="searxng",
        provider_name="SearXNG",
        name="SearXNG",
        base_url="http://localhost:8080",
        model_id="",
        config_type="search",
        enabled=True,
        env_key="",
        api_key_enc=None,
        native_search=False,
        native_search_kind="",
        native_search_detail="",
        native_search_checked_at=None,
    )

    async def fake_enabled(db, config_type):
        return search if config_type == "search" else None

    monkeypatch.setattr(mc, "get_enabled_config", fake_enabled)
    monkeypatch.setattr(mc, "decrypt_api_key", lambda enc: "")

    endpoint = await mc.get_effective_search_endpoint(None)
    assert endpoint["source"] == "service"


@pytest.mark.asyncio
async def test_failed_native_probe_blocks_fallback(monkeypatch):
    llm = _cfg(native_search=False, native_search_checked_at=datetime.now())

    async def fake_enabled(db, config_type):
        return llm if config_type == "llm" else None

    monkeypatch.setattr(mc, "get_enabled_config", fake_enabled)
    monkeypatch.setattr(mc, "decrypt_api_key", lambda enc: "secret")

    assert await mc.get_effective_search_endpoint(None) is None
