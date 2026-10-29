"""长期记忆：一份 Markdown 记忆，所有 Agent 共用。

设计取舍：
- 记忆是给人看、也能手改的 Markdown，而不是黑盒向量库；个人规模下（< 30KB）整段注入
  比 RAG 更可靠、更便宜，也便于用户审阅与纠正。
- 文件分「手写/Agent 归纳」与「自动行为画像」两部分，后者用标记包裹、按周期重建，
  不会被 LLM 反思覆盖。
- 记忆写入是原子替换；反思与写操作都有进程内锁，避免并发轮次互相覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.services.json_utils import extract_json_object

logger = get_logger("memory")

SECTION_TITLES: dict[str, str] = {
    "profile": "用户画像",
    "interests": "兴趣与关注",
    "watch": "跟踪任务",
    "preferences": "阅读偏好",
    "avoid": "需要避免",
    "feedback": "反馈与整改",
    "notes": "Agent 笔记",
}
TITLE_TO_KEY = {title: key for key, title in SECTION_TITLES.items()}
AUTO_TITLE = "行为画像（自动）"
AUTO_START = "<!-- AUTO-PROFILE:START -->"
AUTO_END = "<!-- AUTO-PROFILE:END -->"
PLACEHOLDER = "- （暂无）"

DEFAULT_MEMORY = f"""# 资讯助手记忆

> 这是资讯助手与你共同维护的长期记忆。采集、推荐、简报、对话四个 Agent 都会读取它。
> 你可以直接修改，也可以在对话里说「记住……」让助手更新。

## {SECTION_TITLES['profile']}
{PLACEHOLDER}

## {SECTION_TITLES['interests']}
{PLACEHOLDER}

## {SECTION_TITLES['watch']}
{PLACEHOLDER}

## {SECTION_TITLES['preferences']}
{PLACEHOLDER}

## {SECTION_TITLES['avoid']}
{PLACEHOLDER}

## {SECTION_TITLES['feedback']}
{PLACEHOLDER}

## {SECTION_TITLES['notes']}
{PLACEHOLDER}

## {AUTO_TITLE}
<!-- AUTO-PROFILE:START -->
- 待生成
<!-- AUTO-PROFILE:END -->
"""

AGENT_PRIORITY = {
    "chat": ["watch", "interests", "preferences", "avoid", "profile", "feedback", "notes", "auto"],
    "collect": ["watch", "interests", "avoid", "preferences", "feedback", "profile", "auto"],
    "recommend": ["watch", "interests", "preferences", "avoid", "profile", "feedback", "auto"],
    "brief": ["watch", "interests", "preferences", "profile", "avoid"],
}
AGENT_BUDGET = {"chat": 4200, "collect": 3000, "recommend": 3600, "brief": 2200}

_write_lock = asyncio.Lock()
_reflect_lock = asyncio.Lock()

REASON_LABELS = {
    "helpful": "有帮助",
    "irrelevant": "不相关",
    "shallow": "内容太浅",
    "duplicate": "重复",
    "outdated": "过时",
    "inaccurate": "信息有误",
    "source": "来源不可靠",
    "too_long": "太啰嗦",
    "too_hard": "太难懂",
    "other": "其他",
}

REMEDIES = {
    "irrelevant": "推荐与采集时提高相关性门槛，优先用户关注的主题。",
    "shallow": "采集时优先抓取原文与深度报道，正文补充背景、数据与影响分析。",
    "duplicate": "采集时加强语义去重，同一事件只保留信息最完整的一条。",
    "outdated": "缩短采集时间窗，提高时效性权重，过滤过期内容。",
    "inaccurate": "降低该来源权重，关键事实要求多来源交叉验证并在正文标注不确定处。",
    "source": "降低该来源权重，优先权威媒体与一手信源。",
    "too_long": "回答与简报更精炼：先结论，再要点，减少铺陈。",
    "too_hard": "解释更通俗，补充背景，避免堆砌术语。",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = (read_env_file().get(name, "") or os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = read_env_file().get(name, "") or os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def enabled() -> bool:
    return _env_bool("AGENT_MEMORY_ENABLED", True)


def memory_dir() -> Path:
    raw = (read_env_file().get("AGENT_MEMORY_DIR", "") or os.environ.get("AGENT_MEMORY_DIR", "") or "").strip()
    path = Path(raw).expanduser() if raw else BASE_DIR / "memory"
    return path.resolve()


def memory_path() -> Path:
    return memory_dir() / "PROFILE.md"


def _max_file_chars() -> int:
    return _env_int("AGENT_MEMORY_MAX_CHARS", 60000, 4000, 200000)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _ensure_file() -> None:
    path = memory_path()
    if not path.exists():
        _write_atomic(path, DEFAULT_MEMORY)


def ensure_file() -> None:
    _ensure_file()


def _touch(content: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if re.search(r"^> 最后更新：.*$", content, flags=re.MULTILINE):
        return re.sub(r"^> 最后更新：.*$", f"> 最后更新：{stamp}", content, count=1, flags=re.MULTILINE)
    return content.replace("# 资讯助手记忆\n", f"# 资讯助手记忆\n\n> 最后更新：{stamp}\n", 1)


def _section_bounds(text: str, title: str) -> tuple[int, int] | None:
    heading = f"## {title}"
    start = text.find(heading)
    if start < 0:
        return None
    body_start = text.find("\n", start)
    if body_start < 0:
        return None
    body_start += 1
    match = re.search(r"\n## ", text[body_start:])
    body_end = body_start + match.start() if match else len(text)
    return body_start, body_end


def read_section(text: str, key: str) -> str:
    title = AUTO_TITLE if key == "auto" else SECTION_TITLES.get(key)
    if not title:
        return ""
    bounds = _section_bounds(text, title)
    if not bounds:
        return ""
    return text[bounds[0] : bounds[1]].strip()


def sections(text: str) -> dict[str, str]:
    data = {key: read_section(text, key) for key in SECTION_TITLES}
    data["auto"] = read_section(text, "auto")
    return data


def _clean_lines(body: str) -> list[str]:
    lines = []
    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip() == PLACEHOLDER:
            continue
        if line.lstrip().startswith("<!--"):
            continue
        lines.append(line)
    return lines


def _merge_section(text: str, key: str, new_lines: list[str], *, replace: bool = False, limit: int = 40) -> str:
    """把一个 section 更新为 new_lines（replace=True）或在末尾追加（replace=False）。"""
    title = SECTION_TITLES.get(key)
    if not title:
        return text
    bounds = _section_bounds(text, title)
    if not bounds:
        # 缺 section 时插到自动画像之前
        head = text
        insert_at = text.find(f"## {AUTO_TITLE}")
        if insert_at < 0:
            insert_at = len(text)
        section_text = f"\n## {title}\n" + "\n".join(new_lines) + "\n"
        return head[:insert_at] + section_text + head[insert_at:]

    start, end = bounds
    existing = _clean_lines(text[start:end])
    if replace:
        merged = list(new_lines)
    else:
        merged = existing + list(new_lines)

    seen: set[str] = set()
    unique: list[str] = []
    for line in merged:
        key_norm = re.sub(r"^-\s*(\[\d{4}-\d{2}-\d{2}\]\s*)?", "", line).strip().lower()
        if not key_norm or key_norm in seen:
            continue
        seen.add(key_norm)
        unique.append(line if line.lstrip().startswith(("-", "*")) else f"- {line.strip()}")

    if len(unique) > limit:
        unique = unique[-limit:]

    body = "\n".join(unique) if unique else PLACEHOLDER
    return text[:start] + body + "\n\n" + text[end:]


def write_section(text: str, key: str, body: str, *, replace: bool = True) -> str:
    lines = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line == PLACEHOLDER:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        lines.append(line[:300])
    return _merge_section(text, key, lines, replace=replace)


async def read_memory() -> str:
    _ensure_file()
    try:
        return memory_path().read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取记忆失败：%s", exc)
        return DEFAULT_MEMORY


async def write_memory(content: str) -> str:
    """用户手动保存整份记忆。缺失的固定 section 会补回，自动画像块会被保留/补建。"""
    if not enabled():
        raise ValueError("记忆系统未启用")
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容不能为空")
    if len(content) > _max_file_chars():
        raise ValueError(f"记忆内容过长（上限 {_max_file_chars()} 字）")

    if not content.startswith("# "):
        content = "# 资讯助手记忆\n\n" + content
    for key, title in SECTION_TITLES.items():
        if f"## {title}" not in content:
            content = content.rstrip() + f"\n\n## {title}\n{PLACEHOLDER}\n"
    if AUTO_START not in content or AUTO_END not in content:
        # 用户可能误删了自动画像标记：清掉孤立标记后补一个完整的自动区块
        content = content.replace(AUTO_START, "").replace(AUTO_END, "")
        content = content.rstrip() + f"\n\n## {AUTO_TITLE}\n{AUTO_START}\n- 待生成\n{AUTO_END}\n"
    content = _touch(content.rstrip() + "\n")

    async with _write_lock:
        _write_atomic(memory_path(), content)
    return content


async def reset_memory() -> str:
    if not enabled():
        raise ValueError("记忆系统未启用")
    async with _write_lock:
        _write_atomic(memory_path(), DEFAULT_MEMORY)
    return DEFAULT_MEMORY


async def remember(content: str, section: str = "notes") -> str:
    """追加一条记忆。section 非法时落到 Agent 笔记。"""
    if not enabled():
        raise ValueError("记忆系统未启用")
    content = re.sub(r"\s+", " ", (content or "").strip())
    if not content:
        raise ValueError("要记住的内容不能为空")
    if len(content) > 500:
        content = content[:500] + "…"
    if section not in SECTION_TITLES:
        section = "notes"

    async with _write_lock:
        _ensure_file()
        text = memory_path().read_text(encoding="utf-8")
        line = f"- [{datetime.now().strftime('%Y-%m-%d')}] {content}"
        updated = _merge_section(text, section, [line], replace=False, limit=48)
        updated = _touch(updated)
        if len(updated) > _max_file_chars():
            # 极端情况下保留固定结构，截断各 section 后重写
            logger.warning("记忆文件超过上限，已截断")
            updated = updated[:_max_file_chars()].rstrip() + "\n"
        _write_atomic(memory_path(), updated)
    return read_section(updated, section)


async def forget(query: str) -> int:
    """删除包含 query 的记忆行（不区分大小写），返回删除条数。"""
    if not enabled():
        raise ValueError("记忆系统未启用")
    query = (query or "").strip().lower()
    if not query:
        return 0

    removed = 0
    async with _write_lock:
        _ensure_file()
        text = memory_path().read_text(encoding="utf-8")
        updated = text
        for key in SECTION_TITLES:
            body = read_section(updated, key)
            if not body:
                continue
            lines = _clean_lines(body)
            kept = [line for line in lines if query not in line.lower()]
            removed += len(lines) - len(kept)
            if len(kept) != len(lines):
                updated = _merge_section(updated, key, kept, replace=True)
        if removed:
            _write_atomic(memory_path(), _touch(updated))
    return removed


async def ensure_profile_fresh(db: AsyncSession, *, force: bool = False) -> dict:
    """按周期重建「行为画像（自动）」快照，返回最新画像。"""
    from app.services import user_profile

    if not enabled():
        return {}

    _ensure_file()
    text = await read_memory()

    if not force:
        match = re.search(r"自动生成于\s*([0-9T:\-+\.]+)", read_section(text, "auto"))
        if match:
            try:
                generated = datetime.fromisoformat(match.group(1))
                if datetime.now() - generated < timedelta(hours=6):
                    return {}
            except (ValueError, TypeError):
                pass

    try:
        profile = await user_profile.compute_profile(db)
    except Exception as exc:
        logger.warning("计算用户画像失败：%s", exc)
        return {}

    block = user_profile.render_auto_profile(profile)
    async with _write_lock:
        text = await read_memory()
        body_start = text.find(AUTO_START)
        body_end = text.find(AUTO_END)
        if body_start >= 0 and body_end > body_start:
            updated = text[: body_start + len(AUTO_START)] + "\n" + block + "\n" + text[body_end:]
        elif f"## {AUTO_TITLE}" in text:
            bounds = _section_bounds(text, AUTO_TITLE)
            if bounds:
                updated = text[: bounds[0]] + f"{AUTO_START}\n{block}\n{AUTO_END}\n" + text[bounds[1] :]
            else:
                updated = text.rstrip() + f"\n\n## {AUTO_TITLE}\n{AUTO_START}\n{block}\n{AUTO_END}\n"
        else:
            updated = text.rstrip() + f"\n\n## {AUTO_TITLE}\n{AUTO_START}\n{block}\n{AUTO_END}\n"
        _write_atomic(memory_path(), _touch(updated))
    return profile


def _compact(body: str, budget: int) -> str:
    body = body.strip()
    if not body or body == PLACEHOLDER:
        return ""
    if len(body) <= budget:
        return body
    return body[: max(0, budget - 1)].rstrip() + "…"


async def build_prompt_context(db: AsyncSession, agent: str = "chat") -> str:
    """给某个 Agent 注入的记忆上下文（带预算，避免挤占新闻/对话上下文）。"""
    if not enabled():
        return ""
    try:
        await ensure_profile_fresh(db)
        text = await read_memory()
    except Exception as exc:
        logger.warning("加载记忆失败：%s", exc)
        return ""

    data = sections(text)
    priority = AGENT_PRIORITY.get(agent, AGENT_PRIORITY["chat"])
    budget = AGENT_BUDGET.get(agent, 3200)
    chunks: list[str] = []
    used = 0
    for key in priority:
        body = data.get(key, "")
        title = AUTO_TITLE if key == "auto" else SECTION_TITLES.get(key, key)
        body = _compact(body, max(200, budget - used))
        if not body:
            continue
        chunk = f"## {title}\n{body}"
        if used + len(chunk) > budget:
            break
        chunks.append(chunk)
        used += len(chunk)

    if not chunks:
        return ""
    return "【用户长期记忆（所有 Agent 共用）】\n" + "\n\n".join(chunks)


def extract_focus_terms(memory_text: str, category: str, limit: int = 4) -> list[str]:
    """从记忆里提取与某分类相关的关注词，供搜索规划兜底使用。"""
    body = "\n".join(
        read_section(memory_text, key) for key in ("watch", "interests", "preferences")
    )
    terms: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        plain = re.sub(r"^[-\*]\s*(\[\d{4}-\d{2}-\d{2}\]\s*)?", "", line)
        marker_hit = bool(re.search(rf"[（(\[]\s*{re.escape(category)}\s*[）)\]]", plain))
        if category not in plain and not marker_hit:
            continue
        plain = re.sub(rf"[（(\[]\s*{re.escape(category)}\s*[）)\]]", "", plain)
        plain = re.sub(r"^(跟踪|关注|兴趣|偏好|多推|想看)[:：]\s*", "", plain)
        for part in re.split(r"[、，,；;|/]+", plain):
            term = re.sub(r"^(跟踪|关注|兴趣|偏好|多推|想看)[:：]\s*", "", part).strip(" 。.·")
            term = re.sub(r"\s+", " ", term)
            if "暂无" in term or "待生成" in term:
                continue
            if 2 <= len(term) <= 24 and term not in terms:
                terms.append(term)
            if len(terms) >= limit:
                return terms
    return terms


MEMORY_STOPWORDS = {
    "新闻", "资讯", "内容", "推荐", "文章", "用户", "助手", "关注", "跟踪",
    "以后", "不要", "更多", "一些", "最近", "最新", "可以", "希望", "需要",
    "尽量", "优先", "避免", "少推", "多推", "喜欢", "阅读",
}


def extract_terms(
    memory_text: str,
    sections: tuple[str, ...] = ("watch", "interests"),
    limit: int = 24,
) -> list[str]:
    """提取所有记忆条目中的短词，供推荐打分做兴趣加成 / 避雷惩罚。"""
    body = "\n".join(read_section(memory_text, key) for key in sections)
    terms: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        plain = re.sub(r"^[-\*]\s*(\[\d{4}-\d{2}-\d{2}\]\s*)?", "", line)
        plain = re.sub(r"^(跟踪|关注|兴趣|偏好|多推|少推|想看|避免|不要)[:：]?\s*", "", plain)
        for part in re.split(r"[、，,；;|/\s]+", plain):
            term = re.sub(r"[（(][^）)]*[）)]", "", part)
            term = term.strip(" 。.·（）()[]【】")
            term = re.sub(r"^(跟踪|关注|兴趣|偏好|多推|少推|想看|避免|不要)[:：]?\s*", "", term)
            if not (2 <= len(term) <= 24) or term in MEMORY_STOPWORDS:
                continue
            if "暂无" in term or "待生成" in term:
                continue
            if term not in terms:
                terms.append(term)
            if len(terms) >= limit:
                return terms
    return terms


def format_feedback_line(feedback, detail: dict | None = None) -> str:
    detail = detail or {}
    label = REASON_LABELS.get(feedback.reason, feedback.reason or "未说明")
    target = detail.get("title") or detail.get("excerpt") or feedback.excerpt or "相关内容"
    if len(target) > 60:
        target = target[:60] + "…"
    rating = "认可" if feedback.rating > 0 else "不满意"
    parts = [f"用户对《{target}》{rating}：{label}"]
    if feedback.comment:
        parts.append(f"补充：{feedback.comment[:120]}")
    return "；".join(parts)


async def record_feedback(feedback, detail: dict | None = None) -> str:
    """把反馈写入确定性记忆（不依赖 LLM，保证即使模型不可用也能记住）。"""
    if not enabled():
        return ""
    detail = detail or {}
    line = format_feedback_line(feedback, detail)
    reason = feedback.reason or "other"
    remedy = REMEDIES.get(reason)
    if remedy:
        line += f" → 整改：{remedy}"

    await remember(line, "feedback")

    if feedback.rating < 0 and reason in {"irrelevant", "source", "inaccurate", "shallow"}:
        source = detail.get("source")
        tags = detail.get("tags") or []
        if reason == "source" and source:
            await remember(f"对来源「{source}」保持谨慎，优先交叉验证。", "avoid")
        elif reason == "inaccurate" and source:
            await remember(f"来源「{source}」出现过事实问题，降低权重。", "avoid")
        elif reason == "irrelevant" and tags:
            await remember(f"减少与「{tags[0]}」标签无关的泛化内容。", "avoid")
        elif reason == "shallow":
            await remember("少推只有摘要、没有原文支撑的浅层快讯。", "avoid")

    return line


def _extract_json(text: str) -> dict | None:
    return extract_json_object(text)


async def reflect_pending_feedback(db: AsyncSession) -> dict:
    """Agent 自我反思：读最近反馈与行为画像，归纳并重写记忆中的画像/偏好段落。"""
    from app.models import Feedback, Message, News
    from app.services import llm_client
    from app.services.model_configs import get_active_endpoint

    if not enabled() or not _env_bool("AGENT_MEMORY_REFLECT", True):
        return {"reflected": 0, "reason": "disabled"}

    result = await db.execute(
        select(Feedback)
        .where(Feedback.status.in_(["new", "noted"]), Feedback.attempts < 3)
        .order_by(Feedback.id.desc())
        .limit(20)
    )
    feedbacks = list(result.scalars().all())
    if not feedbacks:
        return {"reflected": 0, "reason": "no_feedback"}

    if not await get_active_endpoint(db, "llm"):
        return {"reflected": 0, "reason": "llm_not_configured"}

    if _reflect_lock.locked():
        return {"reflected": 0, "reason": "busy"}

    async with _reflect_lock:
        text = await read_memory()
        auto = read_section(text, "auto")

        lines: list[str] = []
        for item in feedbacks:
            title = item.excerpt or ""
            if item.target_type == "news" and item.target_id:
                news = (
                    await db.execute(select(News).where(News.id == item.target_id))
                ).scalars().first()
                if news:
                    title = news.title
            elif item.target_type == "message" and item.target_id and not title:
                message = (
                    await db.execute(select(Message).where(Message.id == item.target_id))
                ).scalars().first()
                if message:
                    title = f"助手回答：{message.content[:80]}"
            label = REASON_LABELS.get(item.reason, item.reason or "未说明")
            rating = "认可" if item.rating > 0 else ("不满意" if item.rating < 0 else "中性")
            lines.append(
                f"- [{item.created_at:%Y-%m-%d}] {rating}｜{label}｜对象：{title[:80]}"
                + (f"｜补充：{item.comment[:100]}" if item.comment else "")
            )

        from app.services.agent_prompts import get_agent_prompt

        memory_agent_prompt = await get_agent_prompt(db, "memory")
        system_prompt = (
            (memory_agent_prompt + "\n\n" if memory_agent_prompt else "")
            + "请根据现有记忆、行为画像和最近反馈，输出更新后的记忆段落。\n\n"
            "规则：\n"
            "1. 只输出一个 JSON 对象，不要 Markdown 代码块或解释：\n"
            '   {"profile":"...","interests":"...","watch":"...","preferences":"...","avoid":"...","notes":"..."}\n'
            "2. 每段都是 Markdown 无序列表，每行以 - 开头；每段最多 8 条，每条不超过 80 字。\n"
            "3. 保留现有记忆中的明确任务与事实；除非用户明确否定，不要删除。\n"
            "4. 从反馈中归纳可执行的整改，不要复述原话，不要编造用户未表达的信息。\n"
            "5. 记忆供采集、推荐、简报、对话共用：写清楚「关注什么 / 避免什么 / 怎么写」。\n"
            "6. 不确定就保持原样；宁可少写，也不要臆测。"
        )
        user_prompt = (
            "【现有记忆】\n"
            + "\n\n".join(
                f"## {SECTION_TITLES[key]}\n{read_section(text, key)}"
                for key in ("profile", "interests", "watch", "preferences", "avoid", "notes")
            )
            + f"\n\n【行为画像（自动统计）】\n{auto[:1800]}"
            + "\n\n【最近反馈】\n"
            + "\n".join(lines)
        )

        try:
            answer = await llm_client.chat_text(
                db,
                system_prompt,
                user_prompt,
                purpose="memory",
                temperature=0.2,
                max_tokens=1000,
                timeout=90.0,
            )
            payload = _extract_json(answer)
            if not payload:
                raise ValueError("模型未返回合法 JSON")

            async with _write_lock:
                # 模型调用期间用户可能新增了记忆，写回前重新读取，避免覆盖新内容
                latest = await read_memory()
                updated = latest
                existing_watch = _clean_lines(read_section(updated, "watch"))
                for key in ("profile", "interests", "preferences", "avoid", "notes"):
                    body = payload.get(key)
                    if isinstance(body, str) and body.strip():
                        updated = write_section(updated, key, body, replace=True)
                body = payload.get("watch")
                if isinstance(body, str) and body.strip():
                    updated = write_section(updated, "watch", body, replace=False)
                    # 明确任务必须保留：把原有 watch 条目补回，去重后最多 12 条
                    updated = _merge_section(updated, "watch", existing_watch, replace=False, limit=12)
                _write_atomic(memory_path(), _touch(updated))

            for item in feedbacks:
                item.status = "processed"
                item.processed_at = datetime.now()
                item.attempts = (item.attempts or 0) + 1
            try:
                from app.models import AgentRun

                db.add(
                    AgentRun(
                        started_at=datetime.now(),
                        status="finished",
                        content=json.dumps(
                            [
                                {
                                    "t": "success",
                                    "x": f"记忆反思完成，处理 {len(feedbacks)} 条反馈",
                                    "ts": datetime.now().strftime("%H:%M:%S"),
                                    "agent": "记忆管家",
                                }
                            ],
                            ensure_ascii=False,
                        ),
                    )
                )
            except Exception:
                pass
            await db.flush()
            logger.info("记忆反思完成：处理 %s 条反馈", len(feedbacks))
            return {"reflected": len(feedbacks)}
        except Exception as exc:
            logger.warning("记忆反思失败：%s", exc)
            for item in feedbacks:
                item.attempts = (item.attempts or 0) + 1
                if item.attempts >= 3:
                    item.status = "failed"
            await db.flush()
            return {"reflected": 0, "error": str(exc)}


async def reflect_pending_feedback_background() -> None:
    """在后台用独立 session 反思；失败只记日志，不影响用户请求。"""
    from app.database import get_session

    try:
        async with get_session() as db:
            result = await reflect_pending_feedback(db)
            await db.commit()
            if result.get("reflected"):
                logger.info("后台记忆反思完成：%s", result)
    except Exception as exc:
        logger.warning("后台记忆反思异常：%s", exc)


def status() -> dict:
    path = memory_path()
    try:
        stat = path.stat()
        updated_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
        size = stat.st_size
    except OSError:
        updated_at = None
        size = 0
    return {
        "enabled": enabled(),
        "path": str(path),
        "exists": path.exists(),
        "updated_at": updated_at,
        "size": size,
    }
