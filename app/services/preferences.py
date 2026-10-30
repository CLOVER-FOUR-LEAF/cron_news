"""自然语言偏好分析：把「我关注什么」和「我什么时候有空」变成可执行的采集配置。

设计目标：
- 新用户不需要在自动任务里逐项选择频率、开始时间、提前量或标签；
- 用一段自然语言描述兴趣，模型提取主题标签；
- 用一段自然语言描述时间与阅读量，模型给出偏好时段、兜底间隔、提前量与每次条数；
- 没有配置大模型或分析失败时，用确定性规则兜底，保证向导仍能走完。

分析结果同时写入 .env：
- AGENT_INTERESTS_TEXT / AGENT_INTERESTS_TAGS
- AGENT_SCHEDULE_TEXT / AGENT_SCHEDULE_PLAN
- 智能模式下还会写 TASK_INTERVAL_HOURS / TASK_START_HOUR / TASK_SMART_LEAD_MINUTES
- 两种模式都会写 AGENT_MAX_ITEMS_PER_RUN / AGENT_COLLECT_QUERIES_PER_TAG
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file, write_env_file
from app.logging_setup import get_logger
from app.services import llm_client, tag_service
from app.services.json_utils import extract_json_object

logger = get_logger("preferences")

INTERESTS_TEXT_ENV = "AGENT_INTERESTS_TEXT"
INTERESTS_TAGS_ENV = "AGENT_INTERESTS_TAGS"
SCHEDULE_TEXT_ENV = "AGENT_SCHEDULE_TEXT"
SCHEDULE_PLAN_ENV = "AGENT_SCHEDULE_PLAN"
ANALYZED_AT_ENV = "AGENT_PREFERENCES_ANALYZED_AT"

DEFAULT_HOURS = [8, 21]
DEFAULT_INTERVAL = 8
DEFAULT_LEAD = 45
DEFAULT_ITEMS = 12

# 无模型时的关键词兜底（只覆盖常见领域，模型可用时不会用到）
_TOPIC_KEYWORDS = (
    ("人工智能", ("人工智能", "ai", "大模型", "llm", "aigc", "智能体")),
    ("芯片", ("芯片", "半导体", "算力", "gpu", "光刻")),
    ("财经", ("财经", "金融", "经济", "宏观", "美联储", "利率")),
    ("股市", ("美股", "港股", "a股", "股市", "股票", "财报")),
    ("国际局势", ("国际", "地缘", "外交", "战争", "冲突")),
    ("军事", ("军事", "国防", "军队", "武器")),
    ("科技", ("科技", "互联网", "软件", "云", "开源")),
    ("汽车", ("汽车", "新能源", "电动车", "自动驾驶")),
    ("医药", ("医药", "生物", "医疗", "制药")),
    ("游戏", ("游戏", "电竞", "主机")),
    ("体育", ("体育", "足球", "篮球", "奥运")),
    ("娱乐", ("娱乐", "明星", "影视", "综艺")),
    ("政策", ("政策", "监管", "法规", "政府")),
    ("教育", ("教育", "高校", "考试")),
    ("房地产", ("房地产", "楼市", "房价")),
    ("消费", ("消费", "零售", "电商", "品牌")),
)

_STOP_TAGS = {
    "新闻", "资讯", "热点", "消息", "内容", "文章", "信息", "网站", "平台",
    "关注", "喜欢", "想看", "感兴趣", "领域", "方面", "一些", "每天", "经常",
}

_NEGATIVE_MARKERS = ("不想", "不要", "别推", "避免", "不喜欢", "少推", "厌烦")

_SYSTEM_PROMPT = """你是资讯助手的配置分析师。根据用户的中文自然语言描述，输出严格 JSON：
{
  "interests": {"tags": ["..."], "summary": "一句话总结"},
  "schedule": {
    "preferred_hours": [8, 21],
    "interval_hours": 8,
    "start_hour": 0,
    "lead_minutes": 45,
    "items_per_run": 12
  }
}
规则：
1. tags 为 3-10 个简短主题（2-10 个汉字），要具体到领域/行业/公司/主题；不要“新闻”“资讯”“热点”等泛词，不要整句，不要重复。
2. preferred_hours 是用户通常看资讯的小时（0-23 的整数，最多 6 个，按早到晚排序）。没有明确说明时用 [8, 21]。
3. interval_hours 是兜底采集间隔（1-24 的整数）；start_hour 是固定槽位起点（0-23）；lead_minutes 是活跃时段前预热分钟数（15-120）；items_per_run 是用户一次希望看到的新闻条数（1-80）。
4. 用户如果明确说“不想看/少推”某类内容，不要把它放进 tags。
5. 只输出 JSON，不要 Markdown 代码块，不要解释。"""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def interests_text() -> str:
    return _collapse(read_env_file().get(INTERESTS_TEXT_ENV, ""))


def schedule_text() -> str:
    return _collapse(read_env_file().get(SCHEDULE_TEXT_ENV, ""))


def _load_json(raw: str, default):
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) else default
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def interests_tags() -> list[str]:
    raw = read_env_file().get(INTERESTS_TAGS_ENV, "")
    tags = _load_json(raw, [])
    return tag_service.normalize_tags(tags, limit=10)


def schedule_plan() -> dict:
    raw = read_env_file().get(SCHEDULE_PLAN_ENV, "")
    plan = _load_json(raw, {})
    if not plan:
        return {}
    return _sanitize_plan(plan)


def analyzed_at() -> str:
    return read_env_file().get(ANALYZED_AT_ENV, "")


def snapshot() -> dict:
    plan = schedule_plan()
    return {
        "interests_text": interests_text(),
        "interests_tags": interests_tags(),
        "schedule_text": schedule_text(),
        "schedule_plan": plan,
        "items_per_run": plan.get("items_per_run", DEFAULT_ITEMS),
        "analyzed_at": analyzed_at(),
    }


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _sanitize_hours(values) -> list[int]:
    if isinstance(values, (int, float)):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    hours: list[int] = []
    for raw in values:
        if isinstance(raw, str):
            match = re.search(r"\d{1,2}", raw)
            if not match:
                continue
            raw = match.group(0)
        try:
            hour = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and hour not in hours:
            hours.append(hour)
        if len(hours) >= 6:
            break
    return sorted(hours)


def _sanitize_tags(values) -> list[str]:
    tags = tag_service.normalize_tags(values, limit=10)
    return [tag for tag in tags if 2 <= len(tag) <= 12 and tag not in _STOP_TAGS][:10]


def _sanitize_plan(plan: dict) -> dict:
    preferred = _sanitize_hours(plan.get("preferred_hours"))
    if not preferred:
        preferred = list(DEFAULT_HOURS)
    start = _clamp_int(
        plan.get("start_hour"),
        min(preferred) if preferred else 0,
        0,
        23,
    )
    items = _clamp_int(plan.get("items_per_run"), DEFAULT_ITEMS, 1, 80)
    return {
        "preferred_hours": preferred,
        "interval_hours": _clamp_int(plan.get("interval_hours"), DEFAULT_INTERVAL, 1, 24),
        "start_hour": start,
        "lead_minutes": _clamp_int(plan.get("lead_minutes"), DEFAULT_LEAD, 15, 120),
        "items_per_run": items,
        "queries_per_tag": 3 if items > 12 else 2,
    }


def _fallback_tags(text: str) -> list[str]:
    clauses = re.split(r"[，,、;；。！？!?\n]+", text or "")
    positive_text = "，".join(
        clause for clause in clauses if clause.strip() and not any(
            marker in clause for marker in _NEGATIVE_MARKERS
        )
    )
    lowered = positive_text.lower()
    tags: list[str] = []
    for label, keywords in _TOPIC_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            tags.append(label)
    # 再尝试从逗号分隔的短句中补充；否定分句已经在上一步排除
    for clause in clauses:
        clause = clause.strip()
        if not clause or any(marker in clause for marker in _NEGATIVE_MARKERS):
            continue
        for part in re.split(r"(?:和|与|及|以及|还有)", clause):
            part = re.sub(
                r"^(我|本人|我们)?\s*(比较|很|非常|平时|主要)?\s*(关注|喜欢|想看|感兴趣|在关注|常看)[:：]?\s*",
                "",
                part,
            )
            part = part.strip(" 。.·（）()[]【】\"'“”")
            if 2 <= len(part) <= 10 and part not in _STOP_TAGS:
                tags.append(part)
    return _sanitize_tags(tags) or []


def _fallback_hours(text: str) -> list[int]:
    hours: list[int] = []
    for match in re.finditer(r"(早上|上午|中午|下午|傍晚|晚上|凌晨)?\s*(\d{1,2})\s*[点:：时]", text or ""):
        period, raw = match.group(1) or "", int(match.group(2))
        if period in {"下午", "傍晚", "晚上"} and raw < 12:
            raw += 12
        elif period == "中午" and raw < 11:
            raw += 12
        elif period == "凌晨" and raw == 12:
            raw = 0
        if 0 <= raw <= 23 and raw not in hours:
            hours.append(raw)
    return sorted(hours)[:6] or list(DEFAULT_HOURS)


def _fallback_items(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*[条篇个]", text or "")
    return _clamp_int(match.group(1) if match else DEFAULT_ITEMS, DEFAULT_ITEMS, 1, 80)


def _fallback_plan(schedule: str) -> dict:
    hours = _fallback_hours(schedule)
    items = _fallback_items(schedule)
    return _sanitize_plan(
        {
            "preferred_hours": hours,
            "interval_hours": DEFAULT_INTERVAL,
            "start_hour": min(hours) if hours else 0,
            "lead_minutes": DEFAULT_LEAD,
            "items_per_run": items,
        }
    )


async def analyze_and_save(
    db: AsyncSession,
    interests: str,
    schedule: str,
    mode: str = "smart",
) -> dict:
    interests = _collapse(interests)[:2000]
    schedule = _collapse(schedule)[:2000]
    mode = "smart" if (mode or "").strip().lower() == "smart" else "fixed"

    system_prompt = _SYSTEM_PROMPT
    user_prompt = (
        f"用户兴趣描述：{interests or '（未填写）'}\n\n"
        f"用户时间与阅读量描述：{schedule or '（未填写）'}\n\n"
        "请输出 JSON。"
    )
    analyzed_by = "fallback"
    detail = ""
    payload: dict = {}
    try:
        answer = await llm_client.chat_text(
            db,
            system_prompt,
            user_prompt,
            purpose="preferences",
            temperature=0.2,
            max_tokens=900,
            timeout=60.0,
        )
        payload = extract_json_object(answer) or {}
        if payload:
            analyzed_by = "llm"
    except Exception as exc:
        detail = f"模型分析不可用，已使用规则兜底：{str(exc)[:160]}"
        logger.warning("自然语言偏好分析失败，使用兜底：%s", exc)

    interest_block = payload.get("interests") if isinstance(payload.get("interests"), dict) else {}
    tags = _sanitize_tags(interest_block.get("tags") or [])
    if not tags:
        tags = _fallback_tags(interests)
    summary = _collapse(interest_block.get("summary") or "")[:200]

    schedule_block = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
    if schedule_block:
        plan = _sanitize_plan(schedule_block)
    else:
        plan = _fallback_plan(schedule)
    # 用户没有描述时间时，模型/兜底给出的默认时段仍然可用；但若模型输出了
    # 非法小时，_sanitize_plan 会回退到默认值。
    if not plan.get("preferred_hours"):
        plan["preferred_hours"] = list(DEFAULT_HOURS)

    env = read_env_file()
    env[INTERESTS_TEXT_ENV] = interests
    env[INTERESTS_TAGS_ENV] = json.dumps(tags, ensure_ascii=False)
    env[SCHEDULE_TEXT_ENV] = schedule
    env[SCHEDULE_PLAN_ENV] = json.dumps(plan, ensure_ascii=False)
    env[ANALYZED_AT_ENV] = datetime.now().isoformat(timespec="seconds")

    # 自然语言兴趣接管采集主题；保留用户主动关注的标签（tag_follow）作为补充
    if interests:
        env["TASK_SELECTED_TAGS"] = "[]"
        env["TASK_SELECTED_CATEGORIES"] = "[]"

    env["AGENT_MAX_ITEMS_PER_RUN"] = str(plan["items_per_run"])
    env["AGENT_COLLECT_QUERIES_PER_TAG"] = str(plan["queries_per_tag"])
    if mode == "smart":
        env["TASK_INTERVAL_HOURS"] = str(plan["interval_hours"])
        env["TASK_START_HOUR"] = str(plan["start_hour"])
        env["TASK_SMART_LEAD_MINUTES"] = str(plan["lead_minutes"])
    write_env_file(env)

    return {
        "interests": {"text": interests, "tags": tags, "summary": summary},
        "schedule": plan,
        "applied": mode == "smart",
        "analyzed_by": analyzed_by,
        "detail": detail,
    }
