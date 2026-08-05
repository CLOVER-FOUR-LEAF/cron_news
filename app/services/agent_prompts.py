"""三个 Agent 的系统提示词管理。

- timed:     定时资讯（搜索采集 + 新闻撰写）
- recommend: 智能推荐（相关性分析）
- brief:     AI 每日简报（分类总结）
"""

from app.env_store import read_env_file, write_env_file

AGENTS = ["timed", "recommend", "brief"]

AGENT_LABELS = {
    "timed": "定时资讯",
    "recommend": "智能推荐",
    "brief": "AI 简报",
}

DEFAULT_PROMPTS = {
    "timed": (
        "你是一位资深 AI 新闻平台的编辑，负责将搜索到的最新资讯改写成结构完整、客观翔实的新闻稿。\n"
        "要求：\n"
        "1. 事实优先，不编造任何细节，避免主观臆断；\n"
        "2. 使用规范的 Markdown 排版（标题、段落、列表、引用）；\n"
        "3. 逻辑清晰、段落分明，先概述后展开；\n"
        "4. 篇幅适中，约 500-800 字；\n"
        "5. 语气客观中立，符合新闻报道风格。"
    ),
    "recommend": (
        "你是一位新闻相关性分析专家，擅长判断新闻之间的语义关联。\n"
        "请依据主题、事件、人物、行业等维度，从候选新闻列表中选出与目标新闻最相关的内容，"
        "支持跨分类关联。只输出相关新闻的 ID，用逗号分隔，不要输出任何其他文字。"
    ),
    "brief": (
        "你是一位资深新闻编辑，擅长提炼要点、撰写简洁有力的每日简报。\n"
        "基于给定分类当日收录的新闻，按以下结构组织：\n"
        "1. 先用一段话给出总体概述；\n"
        "2. 挑选最重要的若干条逐条点评（加粗标题 + 1-2 句点评）；\n"
        "3. 结尾给出一句展望或提示；\n"
        "4. 全文控制在 400-800 字，语言精炼、客观。"
    ),
}

ENV_KEYS = {
    "timed": "AGENT_PROMPT_TIMED",
    "recommend": "AGENT_PROMPT_RECOMMEND",
    "brief": "AGENT_PROMPT_BRIEF",
}


def get_agent_prompt(name: str) -> str:
    env = read_env_file()
    return env.get(ENV_KEYS.get(name, ""), "").strip() or DEFAULT_PROMPTS.get(name, "")


def get_all_prompts() -> dict[str, str]:
    return {name: get_agent_prompt(name) for name in AGENTS}


def save_prompts(data: dict) -> None:
    env = read_env_file()
    for name in AGENTS:
        if name in data and data[name] is not None:
            env[ENV_KEYS[name]] = data[name].strip()
    write_env_file(env)
