"""新闻封面：生成方式可配置（文生图 / LLM SVG / 默认图）。

- 读取 `AGENT_COVER_MODE`：image（默认，保持原行为）| svg | off
- `AGENT_COVER_ENABLED` 仍兼容旧配置：为 true 且未设置 mode 时按 image 处理
"""

from __future__ import annotations

import asyncio
import random
import shutil
from pathlib import Path

from app.config import BASE_DIR
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models.news import News
from app.services import svg_cover
from app.services.model_configs import get_active_endpoint

logger = get_logger("cover")

COVER_DIR = BASE_DIR / "images" / "cover"
DEFAULT_COVER_DIR = COVER_DIR / "default"

COVER_MODE_IMAGE = "image"
COVER_MODE_SVG = "svg"
COVER_MODE_OFF = "off"

MODE_LABELS = {
    COVER_MODE_IMAGE: "文生图模型",
    COVER_MODE_SVG: "大模型 SVG",
    COVER_MODE_OFF: "关闭（使用默认封面）",
}


def cover_mode() -> str:
    env = read_env_file()
    raw = (env.get("AGENT_COVER_MODE", "") or "").strip().lower()
    if raw in {COVER_MODE_IMAGE, COVER_MODE_SVG, COVER_MODE_OFF}:
        return raw
    # 兼容旧开关
    return COVER_MODE_IMAGE if env.get("AGENT_COVER_ENABLED", "") == "true" else COVER_MODE_OFF


def _default_cover_paths() -> list[Path]:
    if not DEFAULT_COVER_DIR.exists():
        return []
    return sorted(DEFAULT_COVER_DIR.glob("*.png"))


def _clear_existing(target_base: Path) -> None:
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        path = target_base.with_suffix(suffix)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


async def generate_cover_image(db, title: str, summary: str) -> bytes | None:
    """调用启用的文生图模型生成封面图，失败返回 None。"""
    import httpx

    endpoint = await get_active_endpoint(db, "image")
    if not endpoint or not endpoint["api_key"] or not endpoint["model_id"]:
        return None
    prompt = (
        "为以下新闻生成一张新闻封面图，写实风格，构图清晰，无文字水印，16:9 横向构图：\n"
        f"标题：{title}\n摘要：{summary}"
    )
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{endpoint['base_url']}/images/generations",
            headers={
                "Authorization": f"Bearer {endpoint['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": endpoint["model_id"],
                "prompt": prompt,
                "size": "1024x576",
                "n": 1,
            },
        )
        response.raise_for_status()
        data = response.json()
        item = (data.get("data") or [{}])[0]
        b64 = item.get("b64_json")
        if b64:
            import base64

            return base64.b64decode(b64)
        url = item.get("url")
        if url:
            img = await client.get(url, timeout=60.0)
            img.raise_for_status()
            return img.content
    return None


async def assign_news_cover(db, news: News, emit=None) -> str | None:
    """为新闻分配封面，返回最终的 cover_url。

    - image 模式：调文生图；svg 模式：调大模型生成 SVG
    - 生成失败或 off 模式：随机默认封面
    - SVG 文件与位图分开命名，避免 <img> 因扩展名误判
    """
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    base = COVER_DIR / str(news.id)
    _clear_existing(base)
    mode = cover_mode()
    cover_url: str | None = None

    if mode == COVER_MODE_IMAGE:
        image_bytes = None
        try:
            image_bytes = await generate_cover_image(db, news.title, news.summary or "")
        except Exception as exc:
            if emit:
                await emit("error", f"封面生成失败，改用默认封面: {exc}")
        if image_bytes:
            target = COVER_DIR / f"{news.id}.png"
            target.write_bytes(image_bytes)
            cover_url = f"/images/cover/{news.id}.png"

    elif mode == COVER_MODE_SVG:
        from app.services import tag_service

        tags = news.tag_list
        primary_tag = tags[0] if tags else None
        color = tag_service.tag_color(primary_tag) if primary_tag else None
        try:
            svg = await svg_cover.generate_svg_cover(
                db,
                news.title,
                news.summary or "",
                tag=primary_tag,
                color=color,
            )
        except Exception as exc:
            svg = None
            if emit:
                await emit("error", f"SVG 封面生成失败，改用默认封面: {exc}")
        if svg:
            target = COVER_DIR / f"{news.id}.svg"
            target.write_text(svg, encoding="utf-8")
            cover_url = f"/images/cover/{news.id}.svg"

    if not cover_url:
        candidates = _default_cover_paths()
        if candidates:
            target = COVER_DIR / f"{news.id}.png"
            shutil.copy(str(random.choice(candidates)), str(target))
            cover_url = f"/images/cover/{news.id}.png"

    if cover_url:
        news.cover_url = cover_url
        await db.flush()
    return cover_url


async def assign_covers_batch(db, news_items: list[News], emit=None) -> int:
    """并发生成一批封面（限制并发），返回成功数量。"""
    if not news_items:
        return 0
    sem = asyncio.Semaphore(3)
    done = 0

    async def one(news: News) -> bool:
        nonlocal done
        async with sem:
            try:
                result = await assign_news_cover(db, news, emit)
                if result:
                    done += 1
                return bool(result)
            except Exception as exc:
                logger.warning("封面生成失败（新闻 %s）：%s", news.id, exc)
                return False

    await asyncio.gather(*[one(item) for item in news_items])
    return done
