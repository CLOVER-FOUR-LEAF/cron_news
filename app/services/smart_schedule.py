"""智能定时：根据真实阅读/站内行为学习用户活跃时段，提前预热采集。

设计原则：
- 只做确定性统计，不调用模型；
- 数据来源全部是站内已有时间戳：阅读（read_at / reading_at）、收藏（fav_at）、
  稍后再读（later_at）、对话消息（messages.created_at）、反馈（feedback.created_at）；
- 智能模式仍然保留固定间隔作为「最长等待」兜底，不会因为模型预测而无限期不采集；
- 样本不足时自动退回固定频率，并在状态里说明原因。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import get_logger
from app.models import Feedback, Message, News

logger = get_logger("smart_schedule")

LOOKBACK_DAYS = 30
MIN_EVENTS = 12
MIN_ACTIVE_DAYS = 3
MAX_WINDOWS = 3


@dataclass
class ActivityProfile:
    events: int
    active_days: int
    hour_scores: dict[int, float]
    windows: list[dict] = field(default_factory=list)
    ready: bool = False
    reason: str = ""
    source: str = "none"  # learned=实际行为；plan=用户时间描述；none=学习期
    generated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "source": self.source,
            "events": self.events,
            "active_days": self.active_days,
            "windows": self.windows,
            "reason": self.reason,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "hour_scores": {str(hour): round(score, 2) for hour, score in self.hour_scores.items()},
        }


def _hour_label(hour: int) -> str:
    return f"{hour:02d}:00"


def _window_label(start: int, end: int) -> str:
    if start == end:
        return f"{_hour_label(start)} 前后"
    if end < start:
        return f"{_hour_label(start)}-次日 {_hour_label(end)}"
    return f"{_hour_label(start)}-{_hour_label(end)}"


def _merge_windows(hours: list[int], scores: dict[int, float]) -> list[dict]:
    """把相邻小时合并成活跃窗口，并处理 23 点与 0 点的跨日相邻。"""
    if not hours:
        return []
    ordered = sorted(set(hours))
    groups: list[list[int]] = [[ordered[0]]]
    for hour in ordered[1:]:
        if hour == groups[-1][-1] + 1:
            groups[-1].append(hour)
        else:
            groups.append([hour])

    # 23 点与 0 点相邻：合并成跨日窗口
    if len(groups) > 1 and groups[0][0] == 0 and groups[-1][-1] == 23:
        groups[0] = groups[-1] + groups[0]
        groups.pop()

    windows = []
    for group in groups:
        start = group[0]
        end = (group[-1] + 1) % 24
        windows.append(
            {
                "start": start,
                "end": end,
                "hours": group,
                "label": _window_label(start, end),
                "score": round(max(scores.get(hour, 0.0) for hour in group), 2),
                "weight": round(sum(scores.get(hour, 0.0) for hour in group), 2),
            }
        )
    windows.sort(key=lambda item: item["start"])
    return windows


def _sanitize_preferred_hours(values: Iterable[int] | None) -> list[int]:
    hours: list[int] = []
    for raw in values or []:
        try:
            hour = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and hour not in hours:
            hours.append(hour)
        if len(hours) >= 6:
            break
    return sorted(hours)


def _plan_scores(preferred_hours: list[int]) -> dict[int, float]:
    counts = {hour: 0.0 for hour in range(24)}
    for hour in preferred_hours:
        counts[hour] = 1.0
    return {
        hour: counts[hour]
        + 0.35 * (counts[(hour - 1) % 24] + counts[(hour + 1) % 24])
        for hour in range(24)
    }


def build_profile(
    events: Iterable[tuple[datetime, float]],
    now: datetime | None = None,
    *,
    lookback_days: int = LOOKBACK_DAYS,
    preferred_hours: Iterable[int] | None = None,
) -> ActivityProfile:
    """把行为事件聚合成 24 小时活跃度画像。

    事件权重按 14 天半衰期做指数衰减，避免几个月前的旧习惯长期主导。
    没有足够行为数据时，用配置向导里的「时间描述」临时排程，保证新用户也能
    在描述的活跃时段前预热；一旦行为样本足够，自动切换为实际习惯。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=lookback_days)
    counts = {hour: 0.0 for hour in range(24)}
    active_days: set[date] = set()
    sample = 0

    for timestamp, weight in events:
        if not isinstance(timestamp, datetime):
            continue
        if timestamp > now + timedelta(hours=2):
            continue
        if timestamp < cutoff:
            continue
        age_days = max(0.0, (now - timestamp).total_seconds() / 86400)
        decay = 0.5 ** (age_days / 14.0)
        counts[timestamp.hour] += max(0.05, decay) * float(weight)
        active_days.add(timestamp.date())
        sample += 1

    # 平滑：相邻小时的信号互相加权，避免整点边界把同一习惯拆散
    scores = {
        hour: counts[hour]
        + 0.35 * (counts[(hour - 1) % 24] + counts[(hour + 1) % 24])
        for hour in range(24)
    }
    max_score = max(scores.values()) if scores else 0.0
    learned_ready = sample >= MIN_EVENTS and len(active_days) >= MIN_ACTIVE_DAYS and max_score > 0

    preferred = _sanitize_preferred_hours(preferred_hours)
    if learned_ready:
        threshold = max(0.6, max_score * 0.22)
        # 平滑只用于打分/排序；真正进入窗口的小时必须本小时有行为，避免相邻小时被
        # 平滑值「带进来」，把预热时间提前一两个小时。
        active_hours = [
            hour for hour in range(24) if counts[hour] > 0.05 and scores[hour] >= threshold
        ]
        if not active_hours and max_score > 0:
            active_hours = [max(range(24), key=lambda hour: scores[hour])]
        active_hours = sorted(sorted(active_hours, key=lambda hour: -scores[hour])[:6])
        return ActivityProfile(
            events=sample,
            active_days=len(active_days),
            hour_scores=scores,
            windows=_merge_windows(active_hours, scores)[:MAX_WINDOWS],
            ready=True,
            source="learned",
            reason=f"已学习 {sample} 次站内行为，覆盖 {len(active_days)} 天",
            generated_at=now,
        )

    if preferred:
        plan_scores = _plan_scores(preferred)
        return ActivityProfile(
            events=sample,
            active_days=len(active_days),
            hour_scores=plan_scores,
            windows=_merge_windows(preferred, plan_scores)[:MAX_WINDOWS],
            ready=True,
            source="plan",
            reason="根据你的时间描述临时排程；继续积累阅读行为后会逐步以实际习惯为准",
            generated_at=now,
        )

    if sample == 0:
        reason = "还没有阅读/收藏/对话记录，暂时沿用固定频率"
    else:
        missing_events = max(0, MIN_EVENTS - sample)
        missing_days = max(0, MIN_ACTIVE_DAYS - len(active_days))
        parts = []
        if missing_events:
            parts.append(f"还差 {missing_events} 次行为")
        if missing_days:
            parts.append(f"还差 {missing_days} 天")
        reason = "学习期：" + "、".join(parts) + "，暂时沿用固定频率"
    return ActivityProfile(
        events=sample,
        active_days=len(active_days),
        hour_scores=scores,
        windows=[],
        ready=False,
        source="none",
        reason=reason,
        generated_at=now,
    )


async def collect_activity(
    db: AsyncSession,
    now: datetime | None = None,
    *,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[tuple[datetime, float]]:
    """读取最近一段时间的站内行为时间戳，返回 (时间, 权重)。"""
    now = now or datetime.now()
    since = now - timedelta(days=lookback_days)
    events: list[tuple[datetime, float]] = []

    rows = await db.execute(
        select(News.read_at, News.reading_at, News.fav_at, News.later_at)
        .where(
            News.is_deleted == 0,
            or_(
                News.read_at >= since,
                News.reading_at >= since,
                News.fav_at >= since,
                News.later_at >= since,
            ),
        )
        .order_by(News.id.desc())
        .limit(4000)
    )
    for read_at, reading_at, fav_at, later_at in rows:
        if read_at and read_at >= since:
            events.append((read_at, 1.0))
        if reading_at and reading_at >= since:
            events.append((reading_at, 0.4))
        if fav_at and fav_at >= since:
            events.append((fav_at, 2.0))
        if later_at and later_at >= since:
            events.append((later_at, 1.2))

    # 对话：同一会话同一小时内只计一次，避免一次长聊把某个小时权重拉爆
    rows = await db.execute(
        select(Message.conversation_id, Message.created_at)
        .where(Message.role == "user", Message.created_at >= since)
        .order_by(Message.created_at.desc())
        .limit(2000)
    )
    seen: set[tuple[int, date, int]] = set()
    for conversation_id, created_at in rows:
        if not created_at:
            continue
        key = (int(conversation_id or 0), created_at.date(), created_at.hour)
        if key in seen:
            continue
        seen.add(key)
        events.append((created_at, 1.5))

    rows = await db.execute(
        select(Feedback.created_at)
        .where(Feedback.created_at >= since)
        .order_by(Feedback.created_at.desc())
        .limit(1000)
    )
    for (created_at,) in rows:
        if created_at:
            events.append((created_at, 1.0))

    return events


def next_fixed_slot(now: datetime, interval_hours: int, start_hour: int) -> datetime:
    """固定频率：每日从 start_hour 起，每隔 interval_hours 小时一个槽位。"""
    interval = max(1, int(interval_hours or 1))
    start = int(start_hour or 0) % 24
    best: datetime | None = None
    for day_offset in range(0, 5):
        base = (now + timedelta(days=day_offset)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
        for step in range(0, 120):
            candidate = base + timedelta(hours=step * interval)
            if candidate > now and (best is None or candidate < best):
                best = candidate
                break
    return best or (now + timedelta(hours=interval))


def next_predicted_slot(
    profile: ActivityProfile,
    not_before: datetime,
    lead_minutes: int,
) -> tuple[datetime, dict] | None:
    """返回下一个活跃窗口的「提前预热」时间。"""
    if not profile.ready or not profile.windows:
        return None
    lead = timedelta(minutes=max(0, int(lead_minutes)))
    best: tuple[datetime, dict] | None = None
    for window in profile.windows:
        start_hour = int(window.get("start", 0))
        # 0 点窗口减去提前量会落到前一天，因此从 -1 天开始枚举
        for day_offset in range(-1, 4):
            day = not_before.date() + timedelta(days=day_offset)
            window_start = datetime.combine(day, time(hour=start_hour))
            # 窗口已经开始就跳过；如果只是「提前量已过」但窗口还没开始，
            # 应立即预热（candidate 夹到 not_before），而不是放弃这个窗口。
            if window_start <= not_before:
                continue
            candidate = max(window_start - lead, not_before)
            if candidate > not_before + timedelta(days=4):
                continue
            if best is None or candidate < best[0]:
                best = (candidate, window)
    return best


def next_smart_run(
    profile: ActivityProfile,
    now: datetime,
    interval_hours: int,
    start_hour: int,
    last_run: datetime | None,
    lead_minutes: int = 45,
    min_gap_hours: float | None = None,
) -> tuple[datetime, str]:
    """智能模式下的下一次采集时间。

    取「预测窗口提前量」与「固定周期兜底」中更早的一个；两次采集之间至少间隔 min_gap。
    """
    interval = max(1, int(interval_hours or 1))
    if min_gap_hours is None:
        min_gap_hours = max(0.5, min(float(interval), 2.0))
    earliest = now
    if last_run:
        # 最长等待兜底：无论预测结果如何，超过固定间隔就立即补采
        if now - last_run >= timedelta(hours=interval):
            return now, "已超过最长间隔，立即补采"
        earliest = max(now, last_run + timedelta(hours=min_gap_hours))

    fixed = next_fixed_slot(earliest, interval, start_hour)
    predicted = next_predicted_slot(profile, earliest, lead_minutes)
    if predicted and predicted[0] < fixed - timedelta(minutes=1):
        candidate, window = predicted
        if candidate <= now:
            return candidate, f"活跃窗口 {window.get('label', '')} 即将开始，立即预热"
        return candidate, f"预测活跃窗口 {window.get('label', '')}，提前 {lead_minutes} 分钟预热"
    return fixed, "按固定周期兜底"


def describe_mode(mode: str, interval_hours: int, start_hour: int, lead_minutes: int) -> str:
    if mode == "smart":
        return f"智能模式 · 最长 {interval_hours} 小时 · 活跃前 {lead_minutes} 分钟预热"
    return f"每 {interval_hours} 小时 · 每日 {start_hour} 点起"


def event_hour_counter(events: Iterable[tuple[datetime, float]]) -> Counter:
    """调试/测试辅助：统计各小时事件数（不衰减）。"""
    counter: Counter = Counter()
    for timestamp, _weight in events:
        if isinstance(timestamp, datetime):
            counter[timestamp.hour] += 1
    return counter
