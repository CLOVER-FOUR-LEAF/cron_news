"""用大语言模型生成矢量（SVG）新闻封面。

相比文生图模型，SVG 方案成本约低一到两个数量级，且文件小、可无损缩放。
风险是模型输出的 SVG 可能带脚本或外链，因此必须经过白名单清洗后才能落盘。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from sqlalchemy.ext.asyncio import AsyncSession

from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.services.llm_client import chat

logger = get_logger("svg_cover")

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_SVG_CHARS = 300_000
VIEWBOX = "0 0 1600 900"

# 只保留纯矢量绘图元素；script / foreignObject / image / iframe 等一律移除
ALLOWED_TAGS = {
    "svg",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "defs",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
    "use",
    "title",
    "desc",
}

DEFAULT_SVG_PROMPT = (
    "你是一位新闻视觉设计师，擅长用极简矢量图形表达抽象概念。\n"
    "请为下面的新闻生成一张 16:9 的编辑插画式封面，只输出 SVG 代码。\n\n"
    "设计要求：\n"
    "1. 扁平化、几何化、现代编辑风格，像高质量科技媒体的题图；\n"
    "2. 抽象表达新闻主题（芯片、政策、市场、文化等），不要写实照片感；\n"
    "3. 不要包含任何文字、数字、Logo 或水印；\n"
    "4. 使用渐变色与几何图形营造层次，避免大面积纯黑或纯白；\n"
    "5. 只使用内联定义，禁止引用外部字体、图片、链接或脚本；\n"
    "6. 不要使用 <script>、<foreignObject>、<image>、<use href=\"http...\">。\n\n"
    "输出规范：\n"
    "- 只输出一个完整的 <svg> 元素，根元素包含 xmlns、viewBox=\"0 0 1600 900\"；\n"
    "- 不要输出 Markdown 代码块、解释或任何多余文字。"
)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _strip_code_fence(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"<svg\b.*?</svg>", text, flags=re.S | re.I)
    return match.group(0) if match else text


def _safe_attribute(name: str, value: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("on"):
        return False
    if lowered in {"href", "xlink:href", "src"}:
        return value.strip().startswith("#")
    if lowered == "style":
        style = value.lower()
        if any(token in style for token in ("javascript:", "expression(", "@import")):
            return False
        for url in re.findall(r"url\(([^)]*)\)", style):
            if not url.strip().strip("'\"").startswith("#"):
                return False
    return True


def _clean_element(element: ET.Element) -> bool:
    """递归清洗；返回该元素是否保留。"""
    tag = _local_name(element.tag)
    if tag not in ALLOWED_TAGS:
        return False

    for name in list(element.attrib):
        value = element.attrib.get(name, "")
        if not _safe_attribute(_local_name(name), value):
            element.attrib.pop(name, None)

    kept = []
    for child in list(element):
        if not isinstance(child.tag, str):
            element.remove(child)
            continue
        if _clean_element(child):
            kept.append(child)
        else:
            element.remove(child)
    return True


def sanitize_svg(raw: str) -> str | None:
    """白名单清洗模型输出的 SVG。无法解析或清洗后为空时返回 None。"""
    if not raw or len(raw) > MAX_SVG_CHARS:
        return None
    text = _strip_code_fence(raw)
    if "<svg" not in text.lower():
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        logger.debug("SVG 解析失败：%s", exc)
        return None

    if _local_name(root.tag) != "svg":
        return None
    if not _clean_element(root):
        return None

    viewbox = root.get("viewBox") or root.get("viewbox")
    if not viewbox:
        root.set("viewBox", VIEWBOX)
    root.attrib.pop("viewbox", None)
    root.set("width", "100%")
    root.set("height", "100%")
    root.set("preserveAspectRatio", "xMidYMid slice")

    # 统一由序列化器补 xmlns：先移除元素上已有的，避免输出重复属性
    for name in list(root.attrib):
        if _local_name(name) == "xmlns" or name.startswith("{http://www.w3.org/2000/xmlns/}"):
            root.attrib.pop(name, None)

    try:
        ET.register_namespace("", SVG_NS)
        ET.register_namespace("xlink", XLINK_NS)
        body = ET.tostring(root, encoding="unicode")
    except (ValueError, TypeError) as exc:
        logger.debug("SVG 序列化失败：%s", exc)
        return None

    # 模型输出可能自带 xmlns，序列化后仍可能出现重复，做一次去重
    body = _dedupe_xmlns(body)
    if not body.lstrip().startswith("<svg"):
        return None
    if "xmlns=" not in body[:200]:
        body = body.replace("<svg", f'<svg xmlns="{SVG_NS}"', 1)
    # 双保险：清洗后仍出现危险片段则整体丢弃
    lowered = body.lower()
    if any(token in lowered for token in ("<script", "javascript:", "<foreignobject", "<iframe")):
        return None
    try:
        ET.fromstring(body)
    except ET.ParseError as exc:
        logger.debug("SVG 清洗后不是合法 XML：%s", exc)
        return None
    return body


def _dedupe_xmlns(markup: str) -> str:
    """去掉开始标签里重复的 xmlns 声明，保留第一个。"""
    match = re.search(r"<svg\b[^>]*>", markup, flags=re.I)
    if not match:
        return markup
    tag = match.group(0)
    seen = False
    cleaned = []
    for piece in re.findall(r'\s+[^\s=]+="[^"]*"|\s+[^\s=]+=\'[^\']*\'', tag):
        name = piece.strip().split("=", 1)[0]
        if name in {"xmlns", "xmlns:xlink"}:
            if seen:
                continue
            seen = True
        cleaned.append(piece)
    new_tag = "<svg" + "".join(cleaned) + ">"
    return markup.replace(tag, new_tag, 1)


def _prompt(title: str, summary: str, category: str | None, color: str | None) -> str:
    hint = []
    if category:
        hint.append(f"新闻分类：{category}")
    if color:
        hint.append(f"建议主色调参考 {color}，可自行搭配同色系渐变")
    extra = "\n".join(hint)
    return (
        f"标题：{title}\n"
        f"摘要：{summary[:300] or '（无）'}\n"
        f"{extra}\n\n"
        "请直接输出 SVG。"
    )


async def generate_svg_cover(
    db: AsyncSession,
    title: str,
    summary: str,
    *,
    category: str | None = None,
    color: str | None = None,
) -> str | None:
    """调用当前启用的大语言模型生成封面 SVG；失败返回 None。"""
    system_prompt = read_env_file().get("AGENT_SVG_COVER_PROMPT", "").strip() or DEFAULT_SVG_PROMPT
    try:
        response = await chat(
            db,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _prompt(title, summary, category, color)},
            ],
            purpose="cover_svg",
            temperature=0.85,
            max_tokens=4000,
            timeout=90.0,
        )
    except Exception as exc:
        logger.warning("SVG 封面生成失败：%s", exc)
        return None

    svg = sanitize_svg(response.content)
    if not svg:
        logger.warning("SVG 封面输出无效（清洗未通过）")
    else:
        logger.info("SVG 封面已生成：%s 字符", len(svg))
    return svg
