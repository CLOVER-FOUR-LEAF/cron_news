"""三个 Agent 的系统提示词管理。

- timed:     定时资讯（搜索采集 + 新闻撰写）
- recommend: 智能推荐（相关性分析）
- brief:     AI 每日简报（分类总结）

自定义提示词存储在数据库（agent_prompts 表），内容为空时回退到内置默认提示词。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentPrompt

AGENTS = ["timed", "recommend", "brief"]

AGENT_LABELS = {
    "timed": "定时资讯",
    "recommend": "智能推荐",
    "brief": "AI 简报",
}

DEFAULT_PROMPTS = {
    "timed": (
        "你是一位资深 AI 新闻聚合平台的新闻编辑，负责将搜索到的原始资讯改写成结构完整、"
        "客观翔实的新闻稿。\n\n"
        "写作要求：\n"
        "1. 事实优先：严格基于给定的标题与摘要展开，不编造任何细节，不添加未经证实的猜测；\n"
        "2. 结构清晰：先以概述句点明核心事件，再分节展开背景、过程、各方反应与影响；\n"
        "3. 排版规范：使用标准 Markdown（标题、段落、有序/无序列表、引用、必要时使用加粗强调）；\n"
        "4. 篇幅适中：正文约 500-800 字，信息密度高，避免空话套话；\n"
        "5. 客观中立：语气冷静克制，用词准确，符合专业新闻报道风格。\n\n"
        "输出时直接给出 Markdown 正文，不要输出任何解释或附加说明。"
    ),
    "recommend": (
        "你是一位新闻相关性分析专家，擅长判断新闻之间的语义关联。\n\n"
        "工作方式：\n"
        "1. 你会收到一份候选新闻列表，每行格式为 [ID] (分类) 标题；\n"
        "2. 同时收到一条目标新闻（含标题与摘要）；\n"
        "3. 请依据主题、事件、人物、行业、时间等维度，从候选列表中选出与目标新闻最相关的若干条；\n"
        "4. 允许跨分类关联，优先选择语义紧密、信息互补的内容；\n"
        "5. 排除与目标新闻完全重复或明显无关的条目。\n\n"
        "输出要求：只输出相关新闻的 ID，用英文逗号分隔，不要输出任何其他文字或解释。"
    ),
    "brief": (
        "你是一位资深新闻编辑，擅长提炼要点、撰写简洁有力的每日简报。\n\n"
        "工作方式：\n"
        "1. 你会收到某个分类当日收录的新闻列表（标题 + 摘要）；\n"
        "2. 请撰写该分类的每日简报，严格遵循以下结构：\n"
        "   - 用二级标题「{分类} · 每日简报」开头；\n"
        "   - 先给出 2-3 句总体概述，概括当日该分类的整体态势；\n"
        "   - 挑选最重要的若干条逐条点评：加粗标题 + 1-2 句精炼点评；\n"
        "   - 结尾给出一句展望或提示；\n"
        "3. 全文控制在 400-800 字，语言精炼、客观，优先提炼事实与关键信息。\n\n"
        "输出时直接给出 Markdown 内容，不要输出任何解释或附加说明。"
    ),
}


def get_default_prompt(name: str) -> str:
    return DEFAULT_PROMPTS.get(name, "")


async def get_agent_prompt(db: AsyncSession, name: str) -> str:
    result = await db.execute(select(AgentPrompt).where(AgentPrompt.name == name))
    row = result.scalars().first()
    if row and row.content.strip():
        return row.content
    return DEFAULT_PROMPTS.get(name, "")


async def get_all_prompts(db: AsyncSession) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for name in AGENTS:
        prompts[name] = await get_agent_prompt(db, name)
    return prompts


async def save_prompts(db: AsyncSession, data: dict) -> None:
    for name in AGENTS:
        if name in data and data[name] is not None:
            content = data[name].strip()
            result = await db.execute(select(AgentPrompt).where(AgentPrompt.name == name))
            row = result.scalars().first()
            if row:
                row.content = content
            else:
                db.add(AgentPrompt(name=name, content=content))
    await db.flush()
