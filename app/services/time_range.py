"""中文时间范围解析：把「过去半个月」「上个月」等转换为 since/until。

只做确定性解析，不把模型输出的时间文本直接拼进 SQL。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MAX_DAYS = 365


def _day_start(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time())


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, 28)
    return date(year, month, day)


def _mk(since: datetime, until: datetime, label: str) -> dict:
    now = datetime.now()
    if until > now:
        until = now
    if since > until:
        since = until - timedelta(days=1)
    if (until - since).days > MAX_DAYS:
        since = until - timedelta(days=MAX_DAYS)
    days = max(1, (until - since).days)
    bucket = 1 if days <= 14 else (7 if days <= 60 else 14)
    return {
        "since": since,
        "until": until,
        "label": label,
        "days": days,
        "bucket_days": bucket,
    }


def parse_time_range(expr: str | None, *, now: datetime | None = None) -> dict:
    """解析常见中文时间表达；无法识别时回退到最近 15 天。"""
    now = now or datetime.now()
    text = (expr or "").strip()
    today = now.date()

    if not text:
        return _mk(now - timedelta(days=15), now, "过去 15 天")

    if text in {"过去半个月", "最近半个月", "近半个月", "半个月"}:
        return _mk(now - timedelta(days=15), now, text)

    if text in {"过去半年", "最近半年", "近半年", "半年"}:
        return _mk(now - timedelta(days=180), now, text)

    if text in {"过去一年", "最近一年", "近一年", "一年"}:
        return _mk(now - timedelta(days=365), now, text)

    if text in {"上个月", "上月"}:
        start_this = _month_start(today)
        end_last = start_this
        start_last = _month_start(_add_months(start_this, -1))
        return _mk(_day_start(start_last), _day_start(end_last), text)

    if text in {"本月", "这个月"}:
        return _mk(_day_start(_month_start(today)), now, text)

    if text in {"上周", "上星期"}:
        start_this_week = today - timedelta(days=today.weekday())
        start_last_week = start_this_week - timedelta(days=7)
        return _mk(_day_start(start_last_week), _day_start(start_this_week), text)

    if text in {"本周", "这周", "这星期"}:
        start_this_week = today - timedelta(days=today.weekday())
        return _mk(_day_start(start_this_week), now, text)

    if text in {"今年", "本年度"}:
        return _mk(datetime(today.year, 1, 1), now, text)

    if text in {"去年", "上一年"}:
        return _mk(datetime(today.year - 1, 1, 1), datetime(today.year, 1, 1), text)

    if text in {"今天", "今日"}:
        return _mk(_day_start(today), now, text)

    if text in {"昨天", "昨日"}:
        start = _day_start(today - timedelta(days=1))
        return _mk(start, start + timedelta(days=1), text)

    # 过去/最近/近 N 天|周|月|年（支持阿拉伯数字与中文数字）
    pattern = re.search(
        r"(?:过去|最近|近)\s*([0-9]+|[一二两三四五六七八九十]+)\s*(天|日|周|星期|个月|月|年)",
        text,
    )
    if pattern:
        raw_number = pattern.group(1)
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if raw_number.isdigit():
            number = max(1, int(raw_number))
        elif raw_number in chinese:
            number = chinese[raw_number]
        elif raw_number.startswith("十"):
            number = 10 + chinese.get(raw_number[1:], 0)
        elif "十" in raw_number:
            left, _, right = raw_number.partition("十")
            number = chinese.get(left, 1) * 10 + (chinese.get(right, 0) if right else 0)
        else:
            number = 1
        unit = pattern.group(2)
        if unit in {"天", "日"}:
            delta = timedelta(days=number)
        elif unit in {"周", "星期"}:
            delta = timedelta(weeks=number)
        elif unit in {"个月", "月"}:
            delta = timedelta(days=number * 30)
        else:
            delta = timedelta(days=number * 365)
        return _mk(now - delta, now, text)

    # 显式日期范围
    dates = re.findall(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?", text)
    if len(dates) >= 2:
        try:
            start = date(int(dates[0][0]), int(dates[0][1]), int(dates[0][2]))
            end = date(int(dates[1][0]), int(dates[1][1]), int(dates[1][2]))
            return _mk(_day_start(start), _day_start(end) + timedelta(days=1), text)
        except ValueError:
            pass

    # 「2026年9月」这类单月
    month = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if month:
        try:
            start = date(int(month.group(1)), int(month.group(2)), 1)
            end = _add_months(start, 1)
            return _mk(_day_start(start), _day_start(end), text)
        except ValueError:
            pass

    return _mk(now - timedelta(days=15), now, f"{text}（未识别，按过去 15 天）")


def range_label(since: datetime, until: datetime) -> str:
    return f"{since:%Y-%m-%d} 至 {until:%Y-%m-%d}"
