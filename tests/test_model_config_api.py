"""模型配置接口：测试连接无副作用、保存时写回联网检测结果。"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.database import get_session, init_db
from app.models import ModelConfig
from app.routers import model_configs as router
from app.services.model_configs import encrypt_api_key


async def _make_llm(**overrides) -> int:
    await init_db()
    base = dict(
        provider="deepseek",
        provider_name="DeepSeek",
        name="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        model_id="deepseek-flash",
        config_type="llm",
        enabled=True,
        api_key_enc=encrypt_api_key("sk-test"),
        native_search=True,
        native_search_kind="deepseek",
        native_search_detail="检测成功",
        native_search_checked_at=datetime(2026, 10, 29, 8, 0),
    )
    base.update(overrides)
    async with get_session() as db:
        cfg = ModelConfig(**base)
        db.add(cfg)
        await db.commit()
        return cfg.id


@pytest.mark.asyncio
async def test_create_llm_with_native_probe_persists_id(monkeypatch):
    await init_db()

    async def fake_detect(provider, base_url, api_key, model_id="", timeout=15.0):
        return {"available": True, "kind": "deepseek", "label": "DeepSeek 搜索", "detail": "检测成功"}

    monkeypatch.setattr("app.services.native_search.detect_native_search", fake_detect)

    async with get_session() as db:
        result = await router.create_config(
            router.ModelConfigCreate(
                provider="DeepSeek",
                base_url="https://api.deepseek.com/v1",
                model_id="deepseek-flash",
                config_type="llm",
                api_key="sk-test",
                enabled=True,
            ),
            db=db,
        )
        await db.commit()
    assert result["id"]
    assert result["provider_key"] == "deepseek"
    assert result["native_search"] is True
    assert result["native_search_checked_at"]


@pytest.mark.asyncio
async def test_connection_test_is_side_effect_free(monkeypatch):
    cfg_id = await _make_llm()

    async def fake_probe(config_type, base_url, api_key, model_id, provider=""):
        return True, "连接成功"

    async def fake_detect(provider, base_url, api_key, model_id="", timeout=15.0):
        return {"available": False, "kind": "deepseek", "label": "DeepSeek 搜索", "detail": "联网不可用"}

    monkeypatch.setattr(router, "_probe", fake_probe)
    monkeypatch.setattr("app.services.native_search.detect_native_search", fake_detect)

    async with get_session() as db:
        result = await router.test_connection(
            router.TestConnectionModel(
                config_type="llm",
                config_id=cfg_id,
                provider="deepseek",
                base_url="https://api.deepseek.com/v1",
                model_id="deepseek-flash",
                api_key="",
            ),
            db=db,
        )
        await db.commit()
        assert result["native_search"]["available"] is False

    async with get_session() as db:
        cfg = (await db.execute(select(ModelConfig).where(ModelConfig.id == cfg_id))).scalars().first()
        # 用户可能点「取消」：测试不能把已保存配置的联网能力改坏
        assert cfg.native_search is True
        assert cfg.native_search_detail == "检测成功"
        assert cfg.native_search_checked_at == datetime(2026, 10, 29, 8, 0)


@pytest.mark.asyncio
async def test_save_applies_test_result_even_without_field_changes(monkeypatch):
    cfg_id = await _make_llm(
        provider="mimo",
        provider_name="MiMo（小米）",
        name="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        model_id="mimo-v2.6-pro",
        native_search=True,
        native_search_kind="mimo",
        native_search_detail="检测成功",
        native_search_checked_at=datetime(2026, 10, 29, 8, 0),
    )

    async with get_session() as db:
        result = await router.update_config(
            cfg_id,
            router.ModelConfigUpdate(
                native_search=False,
                native_search_detail="检测失败：联网插件未开通",
            ),
            db=db,
        )
        await db.commit()
        assert result["native_search"] is False

    async with get_session() as db:
        cfg = (await db.execute(select(ModelConfig).where(ModelConfig.id == cfg_id))).scalars().first()
        assert cfg.native_search is False
        assert "联网插件未开通" in cfg.native_search_detail


@pytest.mark.asyncio
async def test_cosmetic_update_keeps_verified_native_search():
    """前端保存时总会带上 provider_name/model_id；只改显示名不能清掉已探测的联网能力。"""
    cfg_id = await _make_llm(
        provider="mimo",
        provider_name="MiMo（小米）",
        name="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        model_id="mimo-v2.6-pro",
        native_search=True,
        native_search_kind="mimo",
        native_search_detail="检测成功",
        native_search_checked_at=datetime(2026, 10, 29, 8, 0),
    )

    async with get_session() as db:
        await router.update_config(
            cfg_id,
            router.ModelConfigUpdate(
                provider="mimo",
                provider_name="MiMo 新名字",
                name="mimo-v2.6-pro",
                base_url="https://api.xiaomimimo.com/v1",
                model_id="mimo-v2.6-pro",
                api_key="",
            ),
            db=db,
        )
        await db.commit()

    async with get_session() as db:
        cfg = (await db.execute(select(ModelConfig).where(ModelConfig.id == cfg_id))).scalars().first()
        assert cfg.provider_name == "MiMo 新名字"
        assert cfg.native_search is True
        assert cfg.native_search_detail == "检测成功"
        assert cfg.native_search_checked_at == datetime(2026, 10, 29, 8, 0)
