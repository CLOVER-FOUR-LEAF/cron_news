"""智能调度：活跃时段学习、固定槽位与预测提前量。"""

from datetime import datetime, timedelta

import pytest

from app.database import get_session, init_db
from app.models import Message, News
from app.services import smart_schedule


def _events_at(hour: int, days: int, *, start: datetime | None = None) -> list[tuple[datetime, float]]:
    base = start or datetime(2026, 10, 1, hour, 0)
    return [(base + timedelta(days=offset), 1.0) for offset in range(days)]


def test_build_profile_learns_active_window():
    now = datetime(2026, 10, 29, 12, 0)
    events = _events_at(8, 12, start=datetime(2026, 10, 14, 8, 30))
    profile = smart_schedule.build_profile(events, now)
    assert profile.ready is True
    assert profile.events == 12
    assert profile.active_days == 12
    assert profile.windows
    assert profile.windows[0]["start"] == 8


def test_build_profile_not_ready_without_signal():
    now = datetime(2026, 10, 29, 12, 0)
    profile = smart_schedule.build_profile([], now)
    assert profile.ready is False
    assert "固定频率" in profile.reason


def test_fixed_slot_uses_interval_and_start():
    now = datetime(2026, 10, 29, 9, 10)
    slot = smart_schedule.next_fixed_slot(now, 8, 0)
    assert slot == datetime(2026, 10, 29, 16, 0)
    slot = smart_schedule.next_fixed_slot(datetime(2026, 10, 29, 23, 0), 8, 0)
    assert slot == datetime(2026, 10, 30, 0, 0)


def test_smart_run_warms_up_before_active_window():
    now = datetime(2026, 10, 29, 6, 0)
    events = _events_at(8, 12, start=datetime(2026, 10, 17, 8, 30))
    profile = smart_schedule.build_profile(events, now)
    target, reason = smart_schedule.next_smart_run(
        profile,
        now,
        interval_hours=8,
        start_hour=0,
        last_run=datetime(2026, 10, 29, 0, 5),
        lead_minutes=45,
    )
    assert target == datetime(2026, 10, 29, 7, 15)
    assert "提前 45 分钟" in reason


def test_smart_run_respects_minimum_gap():
    now = datetime(2026, 10, 29, 7, 0)
    events = _events_at(8, 12, start=datetime(2026, 10, 17, 8, 30))
    profile = smart_schedule.build_profile(events, now)
    target, _ = smart_schedule.next_smart_run(
        profile,
        now,
        interval_hours=8,
        start_hour=0,
        last_run=datetime(2026, 10, 29, 6, 50),
        lead_minutes=45,
        min_gap_hours=2,
    )
    # 7:15 距上次只有 25 分钟，不能再用；下一个固定槽位是 16:00（0/8/16 点）
    assert target == datetime(2026, 10, 29, 16, 0)


def test_smart_run_catches_up_after_max_interval():
    now = datetime(2026, 10, 29, 12, 0)
    profile = smart_schedule.build_profile([], now)
    target, reason = smart_schedule.next_smart_run(
        profile,
        now,
        interval_hours=8,
        start_hour=0,
        last_run=datetime(2026, 10, 29, 1, 0),
    )
    assert target == now
    assert "最长间隔" in reason


def test_predicted_window_runs_immediately_when_lead_already_passed():
    """07:40 重启，08:00 是活跃窗口：不应跳过到明天，而应立即预热。"""
    now = datetime(2026, 10, 29, 7, 40)
    events = _events_at(8, 12, start=datetime(2026, 10, 17, 8, 30))
    profile = smart_schedule.build_profile(events, now)
    assert profile.ready is True
    target, reason = smart_schedule.next_smart_run(
        profile,
        now,
        interval_hours=24,
        start_hour=0,
        last_run=datetime(2026, 10, 28, 18, 0),
    )
    assert target == now
    assert "预热" in reason


def test_aware_last_run_does_not_crash():
    """带时区的 last_run 应被调用方归一化；这里验证纯函数在归一化后正常。"""
    now = datetime(2026, 10, 29, 12, 0)
    profile = smart_schedule.build_profile([], now)
    last_run = datetime(2026, 10, 29, 8, 0)
    target, _ = smart_schedule.next_smart_run(profile, now, 8, 0, last_run)
    assert target > now


def test_window_label_crosses_midnight():
    now = datetime(2026, 10, 29, 12, 0)
    events = []
    for day in range(6):
        day_start = datetime(2026, 10, 20, 0) + timedelta(days=day)
        events.append((day_start.replace(hour=23, minute=10), 1.0))
        events.append((day_start.replace(hour=23, minute=40), 1.0))
    profile = smart_schedule.build_profile(events, now)
    assert profile.ready is True
    assert any("次日" in window["label"] for window in profile.windows)


@pytest.mark.asyncio
async def test_collect_activity_from_database():
    await init_db()
    now = datetime(2026, 10, 29, 12, 0)
    async with get_session() as db:
        for offset in range(12):
            moment = (now - timedelta(days=offset)).replace(hour=8, minute=20, second=0, microsecond=0)
            db.add(
                News(
                    title=f"行为样本 {offset}",
                    is_read=1,
                    read_at=moment,
                    reading_at=moment - timedelta(minutes=3),
                )
            )
        db.add(Message(conversation_id=1, role="user", content="早上好"))
        await db.commit()

    async with get_session() as db:
        events = await smart_schedule.collect_activity(db, now)
    profile = smart_schedule.build_profile(events, now)
    assert profile.events >= 12
    assert profile.ready is True
    assert any(window["start"] == 8 for window in profile.windows)


def test_plan_bootstraps_new_user_without_behavior():
    now = datetime(2026, 10, 29, 12, 0)
    profile = smart_schedule.build_profile([], now, preferred_hours=[8, 21])
    assert profile.ready is True
    assert profile.source == "plan"
    assert [window["start"] for window in profile.windows] == [8, 21]
    assert "时间描述" in profile.reason


def test_learned_behavior_overrides_plan_description():
    now = datetime(2026, 10, 29, 12, 0)
    events = _events_at(10, 12, start=datetime(2026, 10, 17, 10, 30))
    profile = smart_schedule.build_profile(events, now, preferred_hours=[8, 21])
    assert profile.ready is True
    assert profile.source == "learned"
    assert any(window["start"] == 10 for window in profile.windows)
    assert all(window["start"] != 21 for window in profile.windows)


def test_plan_profile_can_schedule_next_run():
    now = datetime(2026, 10, 29, 7, 0)
    profile = smart_schedule.build_profile([], now, preferred_hours=[8, 21])
    target, reason = smart_schedule.next_smart_run(
        profile, now, interval_hours=8, start_hour=8, last_run=datetime(2026, 10, 29, 0, 10)
    )
    assert target == datetime(2026, 10, 29, 7, 15)
    assert "预热" in reason
