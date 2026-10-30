"""辅助模式：批次、查重、更正、初始化、模式切换与 Skill 分发。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import httpx
import pytest

from app.database import init_db
from app.main import app


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("WORK_MODE=assist\nNICKNAME=User123456\n", encoding="utf-8")
    monkeypatch.setattr("app.env_store.ENV_FILE", path)
    return path


@pytest.fixture
def autonomous_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("WORK_MODE=autonomous\nNICKNAME=User123456\n", encoding="utf-8")
    monkeypatch.setattr("app.env_store.ENV_FILE", path)
    return path


@pytest.fixture
def empty_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("WORK_MODE=\nNICKNAME=User123456\n", encoding="utf-8")
    monkeypatch.setattr("app.env_store.ENV_FILE", path)
    return path


@pytest.fixture
async def api():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _token() -> str:
    return uuid.uuid4().hex[:10]


def _news_payload(token: str, batch: str) -> dict:
    return {
        "batch_id": batch,
        "title": f"测试新闻 {token}：某公司发布新芯片",
        "summary": "这是一条用于测试的新闻摘要，说明谁在何时做了什么。",
        "content": "# 测试新闻\n\n这是一条用于测试的新闻正文。",
        "source_url": f"https://example.com/{token}",
        "source": "测试媒体",
        "tags": ["测试标签", f"批次{token}"],
        "credibility": 86,
        "analysis": {
            "key_points": ["要点一", "要点二"],
            "topics": ["测试标签"],
            "why_it_matters": "用于测试。",
            "entities": {"companies": ["某公司"], "people": [], "places": []},
        },
    }


@pytest.mark.asyncio
async def test_news_check_finds_exact_url_beyond_window(api, env_file):
    token = _token()
    payload = _news_payload(token, f"T{token}")
    payload["collected_at"] = (datetime.now() - timedelta(days=90)).isoformat()
    created = await api.post("/api/news", json=payload)
    assert created.status_code == 201, created.text

    check = await api.get(
        "/api/news/check", params={"url": payload["source_url"], "days": 1}
    )
    assert check.status_code == 200
    assert check.json()["exists"] is True
    assert check.json()["match"] == "url"


@pytest.mark.asyncio
async def test_collected_at_timezone_is_normalized(api, env_file):
    token = _token()
    payload = _news_payload(token, f"T{token}")
    payload["collected_at"] = "2026-10-30T00:03:00Z"
    created = await api.post("/api/news", json=payload)
    assert created.status_code == 201, created.text
    # 平台默认 Asia/Shanghai：UTC 00:03 -> 08:03
    assert created.json()["collected_at"].startswith("2026-10-30T08:03")


@pytest.mark.asyncio
async def test_finalize_cover_blocks_protected_assets(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.crud import news as news_crud

    images = tmp_path / "images"
    (images / "cover" / "default").mkdir(parents=True)
    (images / "avatar").mkdir()
    (images / "uploads").mkdir()
    sign = images / "sign.png"
    sign.write_bytes(b"sign")
    avatar = images / "avatar" / "default-avatar.png"
    avatar.write_bytes(b"avatar")
    default = images / "cover" / "default" / "1.png"
    default.write_bytes(b"default")
    other_cover = images / "cover" / "123.png"
    other_cover.write_bytes(b"other")
    upload = images / "uploads" / "ok.png"
    upload.write_bytes(b"upload")

    monkeypatch.setattr(news_crud, "IMAGES_DIR", images)
    monkeypatch.setattr(news_crud, "COVER_DIR", images / "cover")
    news = SimpleNamespace(id=999)

    assert await news_crud.finalize_cover(news, "/images/sign.png") is None
    assert await news_crud.finalize_cover(news, "/images/avatar/default-avatar.png") is None
    assert await news_crud.finalize_cover(news, "/images/cover/default/1.png") is None
    assert await news_crud.finalize_cover(news, "/images/cover/123.png") is None
    assert sign.is_file()
    assert avatar.is_file()
    assert default.is_file()
    assert other_cover.is_file()

    result = await news_crud.finalize_cover(news, "/images/uploads/ok.png")
    assert result == "/images/cover/999.png"
    assert (images / "cover" / "999.png").is_file()
    # 复制而不是移动：重试时源文件仍在
    assert upload.is_file()


@pytest.mark.asyncio
async def test_news_urls_are_validated(api, env_file):
    token = _token()
    payload = _news_payload(token, f"T{token}")
    payload["source_url"] = "javascript:alert(1)"
    bad_source = await api.post("/api/news", json=payload)
    assert bad_source.status_code == 422

    payload = _news_payload(token, f"T{token}")
    payload["cover_url"] = 'x" onerror="alert(1)'
    bad_cover = await api.post("/api/news", json=payload)
    assert bad_cover.status_code == 422

    payload = _news_payload(token, f"T{token}")
    payload["cover_url"] = f"/images/uploads/{token}.png"
    ok = await api.post("/api/news", json=payload)
    assert ok.status_code == 201, ok.text
    assert ok.json()["cover_url"] == f"/images/uploads/{token}.png"


@pytest.mark.asyncio
async def test_onboarding_rejects_unknown_sections_and_empty_payload(api, env_file):
    unknown = await api.post(
        "/api/assist/onboarding", json={"sections": {"interest": "- typo"}}
    )
    assert unknown.status_code == 400

    empty = await api.post("/api/assist/onboarding", json={})
    assert empty.status_code == 400
    status = (await api.get("/api/assist/status")).json()
    assert status["onboarded"] is False


@pytest.mark.asyncio
async def test_news_batch_feed_check_update_delete(api, env_file):
    token = _token()
    batch = f"T{token}"
    payload = _news_payload(token, batch)

    created = await api.post("/api/news", json=payload)
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["batch_id"] == batch
    assert data["credibility"] == 86
    news_id = data["id"]

    feed = await api.get("/api/assist/feed?days=1&limit=100")
    assert feed.status_code == 200
    feed_data = feed.json()
    assert any(item["id"] == news_id for item in feed_data["items"])

    by_url = await api.get("/api/news/check", params={"url": payload["source_url"]})
    assert by_url.status_code == 200
    assert by_url.json()["exists"] is True
    assert by_url.json()["match"] == "url"

    by_title = await api.get("/api/news/check", params={"title": payload["title"]})
    assert by_title.status_code == 200
    assert by_title.json()["exists"] is True
    assert by_title.json()["match"] == "title"

    patched = await api.patch(
        f"/api/news/{news_id}",
        json={"title": f"更新后的标题 {token}", "tags": [f"更新标签{token}"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == f"更新后的标题 {token}"
    assert patched.json()["tags"] == [f"更新标签{token}"]

    deleted = await api.delete(f"/api/news/{news_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    missing = await api.get(f"/api/news/{news_id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_assist_onboarding_and_status(api, env_file):
    token = _token()
    tag = f"初始化标签{token}"
    payload = {
        "version": 1,
        "agent": "test-agent",
        "nickname": "小明",
        "sections": {
            "profile": f"- 测试画像 {token}",
            "interests": f"- {tag}",
            "watch": f"- 跟踪 {tag}",
            "preferences": "- 每天早晚各一次，每次 12 条",
            "avoid": "- 娱乐八卦",
            "notes": "- 初始化测试",
        },
        "tags": [tag, "AI芯片"],
        "schedule": {"timezone": "Asia/Shanghai", "times": ["08:00", "20:00"], "items_per_run": 12},
        "editorial": {"depth": "deep", "article_length": [600, 900], "cover": "off"},
    }
    resp = await api.post("/api/assist/onboarding", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["memory_saved"] is True
    assert tag in data["tags"]
    assert data["nickname"] == "小明"

    status = await api.get("/api/assist/status")
    assert status.status_code == 200
    snapshot = status.json()
    assert snapshot["work_mode"] == "assist"
    assert snapshot["onboarded"] is True
    assert snapshot["onboarding"]["schedule"]["times"] == ["08:00", "20:00"]
    assert snapshot["onboarding"]["editorial"]["depth"] == "deep"
    assert snapshot["nickname"] == "小明"

    memory = await api.get("/api/memory")
    assert memory.status_code == 200
    sections = memory.json()["sections"]
    assert tag in sections["interests"]
    assert "外部 Agent 初始化完成" in sections["notes"]

    tags = await api.get("/api/tags?limit=100")
    assert tag in tags.json()["followed"]


@pytest.mark.asyncio
async def test_onboarding_repeat_preserves_state_and_can_reset(api, env_file):
    token = _token()
    tag = f"保留标签{token}"
    first = {
        "agent": "test-agent",
        "sections": {"interests": f"- {tag}"},
        "tags": [tag],
        "schedule": {"times": ["08:00", "20:00"], "items_per_run": 12},
        "editorial": {"depth": "deep", "cover": "off"},
    }
    created = await api.post("/api/assist/onboarding", json=first)
    assert created.status_code == 200, created.text

    # 只更新 sections，不传 tags/schedule/editorial：应保留上次状态
    second = {"sections": {"watch": f"- 跟踪 {tag}"}}
    updated = await api.post("/api/assist/onboarding", json=second)
    assert updated.status_code == 200, updated.text
    snapshot = (await api.get("/api/assist/status")).json()
    assert snapshot["onboarding"]["schedule"]["times"] == ["08:00", "20:00"]
    assert snapshot["onboarding"]["editorial"]["depth"] == "deep"
    assert tag in snapshot["onboarding"]["tags"]

    reset = await api.delete("/api/assist/onboarding")
    assert reset.status_code == 200
    snapshot = (await api.get("/api/assist/status")).json()
    assert snapshot["onboarded"] is False
    # 只清除初始化状态，不动关注标签
    tags = (await api.get("/api/tags?limit=100")).json()
    assert tag in tags["followed"]


@pytest.mark.asyncio
async def test_onboarding_rejected_in_autonomous(api, autonomous_env):
    resp = await api.post("/api/assist/onboarding", json={"sections": {"interests": "- X"}})
    assert resp.status_code == 409
    assert "辅助模式" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_memory_patch_appends_and_replaces(api, env_file):
    token = _token()
    first = f"- 旧兴趣 {token}"
    second = f"- 新兴趣 {token}"

    resp = await api.patch("/api/memory", json={"mode": "append", "sections": {"interests": first}})
    assert resp.status_code == 200, resp.text
    resp = await api.patch("/api/memory", json={"mode": "append", "sections": {"interests": second}})
    assert resp.status_code == 200
    sections = resp.json()["sections"]
    assert first in sections["interests"]
    assert second in sections["interests"]

    resp = await api.patch(
        "/api/memory",
        json={"mode": "replace", "sections": {"interests": f"- 只保留 {token}"}},
    )
    assert resp.status_code == 200
    assert f"只保留 {token}" in resp.json()["sections"]["interests"]
    assert first not in resp.json()["sections"]["interests"]

    invalid = await api.patch("/api/memory", json={"sections": {"feedback": "- 不应写入"}})
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_feedback_ack(api, env_file, monkeypatch):
    monkeypatch.setattr("app.services.feedback_service.schedule_reflection", lambda: None)
    created = await api.post(
        "/api/feedback",
        json={"target_type": "chat", "rating": -1, "reason": "shallow", "comment": "测试反馈"},
    )
    assert created.status_code == 201, created.text
    feedback_id = created.json()["id"]

    listed = await api.get("/api/feedback?limit=20")
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()["items"]]
    assert feedback_id in ids

    acked = await api.post(f"/api/feedback/{feedback_id}/ack")
    assert acked.status_code == 200
    assert acked.json()["status"] == "processed"


@pytest.mark.asyncio
async def test_home_template_switches_and_data_survives(api, env_file):
    token = _token()
    created = await api.post("/api/news", json=_news_payload(token, f"T{token}"))
    assert created.status_code == 201
    news_id = created.json()["id"]

    assist_home = await api.get("/")
    assert assist_home.status_code == 200
    assert "wall-page" in assist_home.text
    assert "资讯墙" in assist_home.text

    switched = await api.put("/api/config", json={"work_mode": "autonomous"})
    assert switched.status_code == 200
    autonomous_home = await api.get("/")
    assert autonomous_home.status_code == 200
    assert "wall-page" not in autonomous_home.text
    assert "home-full" in autonomous_home.text

    # 切换不迁移、不删除数据
    still_there = await api.get(f"/api/news/{news_id}")
    assert still_there.status_code == 200

    back = await api.put("/api/config", json={"work_mode": "assist"})
    assert back.status_code == 200
    assist_again = await api.get("/")
    assert "wall-page" in assist_again.text
    still_there = await api.get(f"/api/news/{news_id}")
    assert still_there.status_code == 200


@pytest.mark.asyncio
async def test_invalid_work_mode_rejected(api, env_file):
    resp = await api.put("/api/config", json={"work_mode": "turbo"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_assist_mode_rejects_scheduler_trigger(api, env_file):
    resp = await api.post("/api/scheduler/trigger")
    assert resp.status_code == 409
    assert "不是自主模式" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_scheduler_trigger_rejected_when_mode_empty(api, empty_env):
    assert (await api.post("/api/scheduler/trigger")).status_code == 409
    assert (await api.get("/api/scheduler/trigger/stream")).status_code == 409


@pytest.mark.asyncio
async def test_assist_mode_blocks_internal_agent_jobs(api, env_file):
    from app.services import agent_jobs, embeddings
    from app.services.scheduler import scheduler

    result = await agent_jobs.run_collection()
    assert result["ok"] is False
    assert result.get("assist_blocked") is True

    with pytest.raises(RuntimeError):
        await scheduler.trigger_now()

    refresh = await api.post("/api/memory/refresh")
    assert refresh.status_code == 409

    chat = await api.post("/api/chat/stream", json={"message": "你好"})
    assert chat.status_code == 409

    analyze = await api.post(
        "/api/config/preferences/analyze", json={"interests_text": "AI 芯片"}
    )
    assert analyze.status_code == 409

    backfill = await embeddings.backfill()
    assert backfill.get("reason") == "assist_mode"

    search = await api.get("/api/retrieval/search", params={"q": "测试", "mode": "hybrid"})
    assert search.status_code == 200
    assert search.json()["mode"] == "lexical"


@pytest.mark.asyncio
async def test_assist_mode_does_not_schedule_feedback_reflection(api, env_file, monkeypatch):
    from app.services import memory_service

    called = []
    monkeypatch.setattr(memory_service, "reflect_pending_feedback_background", lambda: called.append(True))
    resp = await api.post(
        "/api/feedback",
        json={"target_type": "chat", "rating": -1, "reason": "shallow", "comment": "测试"},
    )
    assert resp.status_code == 201
    assert called == []


@pytest.mark.asyncio
async def test_skill_serving_and_manifest(api, env_file):
    resp = await api.get("/api/skill")
    assert resp.status_code == 200
    assert "name: cron-news" in resp.text
    assert "version: 1.0.0" in resp.text

    manifest = await api.get("/api/skill/manifest")
    assert manifest.status_code == 200
    data = manifest.json()
    assert data["version"] == "1.0.0"
    paths = {item["path"] for item in data["files"]}
    assert "SKILL.md" in paths
    assert "references/api.md" in paths
    assert "references/onboarding.md" in paths
    assert "scripts/cron_news.py" in paths

    reference = await api.get("/api/skill", params={"path": "references/api.md"})
    assert reference.status_code == 200
    assert "/api/assist/status" in reference.text

    traversal = await api.get("/api/skill", params={"path": "../.env"})
    assert traversal.status_code in (400, 404)


@pytest.mark.asyncio
async def test_media_upload_and_reject_fake_image(api, env_file, tmp_path, monkeypatch):
    import app.routers.media as media_router

    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(media_router, "UPLOAD_DIR", upload_dir)

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    ok = await api.post("/api/media", files={"file": ("cover.png", png, "image/png")})
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["url"].startswith("/images/uploads/")
    assert (upload_dir / data["filename"]).is_file()

    fake = await api.post("/api/media", files={"file": ("x.png", b"<html>hi</html>", "image/png")})
    assert fake.status_code == 400


@pytest.mark.asyncio
async def test_home_feed_can_skip_recommendations(api, env_file):
    token = _token()
    created = await api.post("/api/news", json=_news_payload(token, f"T{token}"))
    assert created.status_code == 201

    resp = await api.get("/api/home?limit=50&include_recommendations=false")
    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"] == []
    assert data["recommend_hint"] == ""
