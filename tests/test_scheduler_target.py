"""调度器时间计算：失败退避、外部采集时间、时区归一化。"""

from datetime import datetime, timedelta

import importlib

import pytest

from app.services import smart_schedule

scheduler_module = importlib.import_module("app.services.scheduler")
Scheduler = scheduler_module.Scheduler


def _ready_profile(now: datetime):
    events = []
    for day in range(12):
        events.append((now - timedelta(days=day), 1.0))
        events.append((now - timedelta(days=day, minutes=-20), 1.0))
    return smart_schedule.build_profile(events, now)


@pytest.mark.asyncio
async def test_failed_attempt_prevents_immediate_retry(monkeypatch):
    now = datetime.now().replace(microsecond=0)
    sched = Scheduler()
    monkeypatch.setattr(sched, "_read_task_settings", lambda: (8, 0))
    monkeypatch.setattr(sched, "_schedule_mode", lambda: "smart")
    monkeypatch.setattr(sched, "_lead_minutes", lambda: 45)
    monkeypatch.setattr(scheduler_module, "read_env_file", lambda: {})

    async def fake_profile():
        return _ready_profile(now)

    monkeypatch.setattr(sched, "_get_smart_profile", fake_profile)
    sched._last_run = now - timedelta(hours=9)
    sched._last_attempt = now - timedelta(minutes=1)

    target = await sched._compute_target(now)
    # 最近一次尝试才过去 1 分钟，不能因为「上次成功在 9 小时前」就立刻重试
    assert target > now


@pytest.mark.asyncio
async def test_last_run_uses_newer_external_collection_time(monkeypatch):
    now = datetime.now().replace(microsecond=0)
    sched = Scheduler()
    sched._last_run = now - timedelta(hours=9)
    external = now - timedelta(minutes=30)
    monkeypatch.setattr(
        scheduler_module,
        "read_env_file",
        lambda: {"LAST_SEARCH_TIME": external.isoformat()},
    )
    assert sched._last_run_dt() == external


def test_parse_datetime_normalizes_timezone_and_rejects_bad_values(monkeypatch):
    parsed = scheduler_module._parse_datetime("2026-10-29T10:00:00+08:00")
    assert parsed is not None
    assert parsed.tzinfo is None
    assert scheduler_module._parse_datetime("not-a-date") is None
    assert scheduler_module._parse_datetime("") is None


def test_stale_check_handles_future_timestamp(monkeypatch):
    sched = Scheduler()
    future = (datetime.now() + timedelta(hours=3)).isoformat()
    monkeypatch.setattr(scheduler_module, "read_env_file", lambda: {"LAST_SEARCH_TIME": future})
    # 未来时间不应被判定为「过期」，否则会不断触发补跑
    assert sched._stale_since_last_run(8) is False
