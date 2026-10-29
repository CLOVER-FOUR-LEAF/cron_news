"""内容质量闸门：判断候选结果到底是不是「一篇新闻文章」。

采集链路的搜索结果经常混入门户首页、栏目页、登录页、聚合直播页、维基百科、
应用商店与推广软文。这些页面在搜索排序里可能很靠前，但绝不是新闻。
本模块只做确定性规则判断（不调用模型），负责在抓取与撰稿之前把它们挡掉；
模型侧的二次判定见 ai_service.analyze_article 的 is_news 契约。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

# 路径中出现这些片段，基本可以判定为栏目页 / 登录页 / 列表页 / 视频页
_NON_ARTICLE_PATH_HINTS = (
    "/cate/", "/category/", "/categories/", "/channel/", "/channels/",
    "/topic/", "/topics/", "/list/", "/search", "/login", "/signin",
    "/sign-in", "/signup", "/sign-up", "/register", "/account/",
    "/accounts/", "/mail", "/store", "/apps/", "/download", "/video",
    "/videos/", "/videoid/", "/playlist", "/live", "/special/",
    "/zhuanti/", "/kuaixun", "/roll/", "/index", "/home",
    "/channel", "/tag/", "/tags/", "/rank/", "/trends/", "/hotnews/",
    "/instant-news", "/newsreallist", "/node_", "/z/", "/keyword",
    "/keywords/", "/关键词/", "/chat_history",
)

# 拼音栏目名（路径最后一段包含这些片段即为入口页）
_NON_ARTICLE_SLUG_HINTS = (
    "xinwen", "zixun", "caijing", "junshi", "keji", "shipin",
    "zhibo", "toutiao", "redian", "zhuanlan", "dongtai",
)

# 路径最后一段是这些词，说明打开的是入口页而不是文章
_NON_ARTICLE_SEGMENTS = {
    "", "/", "index", "index.html", "index.htm", "home", "homepage",
    "news", "latest", "list", "all", "more", "category", "categories",
    "cate", "channel", "channels", "topic", "topics", "tag", "tags",
    "search", "login", "signin", "sign-in", "signup", "sign-up",
    "register", "account", "accounts", "mail", "store", "apps",
    "download", "video", "videos", "live", "special", "zhuanti",
    "roll", "kuaixun", "newslist",
}

# 整站都不适合作为单条新闻的域名（账号页、百科、视频/社交平台、应用商店）
_BLOCKED_DOMAINS = {
    "wikipedia.org", "wikiwand.com", "fandom.com", "baike.baidu.com",
    "baike.sogou.com", "baike.so.com", "baike.com",
    "youtube.com", "youtu.be", "bilibili.com", "douyin.com",
    "x.com", "twitter.com", "facebook.com", "instagram.com",
    "account.microsoft.com", "myaccount.microsoft.com",
    "apps.microsoft.com", "login.microsoftonline.com",
    "outlook.office.com", "store.steampowered.com",
    "apps.apple.com", "play.google.com",
}

_EVENT_WORDS = (
    "发布", "宣布", "推出", "上调", "下调", "收购", "合并", "裁员", "起诉",
    "调查", "袭击", "爆炸", "签署", "通过", "批准", "财报", "营收", "增长",
    "下降", "警告", "计划", "启动", "回应", "否认", "承认", "辞职", "当选",
    "达成", "突破", "部署", "发射", "测试", "召回", "处罚", "禁令", "谈判",
    "会谈", "制裁", "降息", "加息", "违约", "获奖", "融资", "上市", "退市",
    "上涨", "下跌", "获批", "亮相", "开售", "交付", "合作", "披露",
)

# 栏目/导航类标题（整词匹配，去掉空白与标点后比较）
_GENERIC_TITLES = {
    "科技", "科技创新", "科技动态", "科技新闻", "科技资讯",
    "财经", "财经新闻", "财经资讯", "财经首页", "财经头条",
    "经济", "经济新闻", "军事", "军事新闻", "军事资讯",
    "国际", "国际新闻", "国内", "国内新闻", "社会", "社会新闻",
    "新闻", "资讯", "热点", "要闻", "头条", "头条新闻", "综合",
    "首页", "主页", "视频", "直播", "专题", "更多", "快讯", "滚动",
    "outlook", "mail", "news", "latest", "home", "login", "sign in",
    "signin", "wikipedia", "microsoft", "store",
}


def _normalize_for_compare(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)


_GENERIC_TITLES_NORMALIZED = frozenset(_normalize_for_compare(item) for item in _GENERIC_TITLES)

_LOGIN_RE = re.compile(
    r"(?i)^\s*\[?\s*(sign\s*in|log\s*in|login|sign\s*up|register|"
    r"create\s+(your\s+)?account|access\s+your\s+account)"
)
_EN_PORTAL_RE = re.compile(
    r"(?i)\b(sign\s*in|log\s*in|sign\s*up|create\s+(your\s+)?account|"
    r"download\s+apps|app\s+store|wikipedia|online\s+services)\b"
)
_WIKI_RE = re.compile(r"(-\s*维基百科|[-—]\s*Wikipedia)\s*$", re.I)
_PROMO_RE = re.compile(
    r"(?i)^\s*\[?\s*(featured|ad|ads|sponsored|promoted|广告|推广|赞助)\s*\]?"
    r"|[\|｜]\s*(广告|推广|赞助)\s*$"
)
# 强门户标记：真实新闻标题几乎不会出现
_PORTAL_MARK_RE = re.compile(
    r"(首页|首頁|新闻中心|新聞中心|新闻频道|新聞頻道|资讯频道|資訊頻道|"
    r"滚动新闻|滾動新聞|滚动播报|实时播报|实时快讯|"
    r"7\s*[×xX]\s*24|24\s*小时直播|24\s*小時直播|直播间|直播間|直播中|"
    r"最新报道|最新報導|最新资讯|最新資訊|最新消息|最新新闻|最新新聞|"
    r"新闻汇总|新聞匯總|资讯汇总|資訊匯總|快讯汇总|官方网站|官方網站|"
    r"企业官网|產品中心|产品中心|解决方案|解決方案|应用商店|應用商店|"
    r"下载中心|下載中心|登录页|登錄頁)"
)
# 结尾是「…新闻-某某网 / 某某社 / 某某频道」这类站点标题
_SITE_TAIL_RE = re.compile(
    r"(新闻|新聞|资讯|資訊|财经|財經|科技|军事|軍事|体育|體育|娱乐|娛樂|"
    r"汽车|汽車|房产|房產|教育|健康|文化|旅游|旅遊|"
    r"时尚|時尚|游戏|遊戲|数码|數碼|科学|科學|热点|熱點|要闻|要聞|首页|首頁)"
    r"[-_|｜—–\s]*(网|網|网站|網站|门户|門戶|频道|頻道|中心|社|报社|報社|电视台|電視台)\s*$"
)
_LIST_MARK_RE = re.compile(r"[·•]\s*")
# 频道名 + 分隔符 + 站点名（如「科技新聞｜香港01」「财经新闻_最新财经要闻_澎湃新闻」）
_CATEGORY_WORD_RE = re.compile(
    r"(新闻|新聞|资讯|資訊|财经|財經|经济|經濟|科技|军事|軍事|国际|國際|"
    r"国内|國內|体育|體育|娱乐|娛樂|社会|社會|文化|教育|健康)"
)
_TITLE_SEPARATOR_RE = re.compile(r"[|｜_—–]|：|:")
# 榜单 / 热榜 / 实时更新这类浏览页标记
_BROWSE_MARK_RE = re.compile(
    r"(热点|熱點|热榜|熱榜|热搜|熱搜|排行|排行榜|滚动|滾動|实时更新|實時更新|"
    r"即时新闻|即時新聞|最新动态|最新動態|最新信息|最新資訊|最新要闻|最新要聞|"
    r"视频和分析|視頻和分析|播客|图集|专题|專題)"
)

# 标题尾部常见站点名，成稿前剥掉
_SITE_SUFFIX_RE = re.compile(
    r"\s*[-_|｜—–]\s*(新浪(财经|科技|新闻)?|网易(科技|财经|新闻)?|腾讯(新闻|科技|财经)?|"
    r"搜狐(新闻|科技|财经)?|凤凰(网|科技|财经)?|东方财富(网)?|和讯(网)?|同花顺|"
    r"财新(网)?|财联社|澎湃(新闻)?|界面(新闻)?|第一财经|每日经济新闻|经济观察网|"
    r"21世纪经济报道|证券时报|中国新闻网|新华网|人民网|央视网|环球网|观察者网|"
    r"参考消息|36氪|虎嗅(网)?|钛媒体|雷峰网|极客公园|IT之家|快科技|"
    r"Reuters|Bloomberg|BBC|CNN|TechCrunch|The Verge|Wired|Ars Technica|"
    r"New York Times|Financial Times|The Wall Street Journal)\s*$"
    r"|\s*[_|｜]\s*7\s*[x×X]\s*24\s*小时财经新闻\s*[_|｜]\s*新浪网\s*$"
    r"|\s*[-_|｜—–]\s*(新闻|资讯)\s*$",
    re.I,
)


def _domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_blocked(domain: str) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in _BLOCKED_DOMAINS)


def clean_title(title: str | None) -> str:
    """清理标题：去掉站点后缀、推广标记与多余空白，不做语义改写。"""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    text = _PROMO_RE.sub("", text).strip()
    text = _SITE_SUFFIX_RE.sub("", text).strip()
    text = text.strip("\"'“”‘’《》 ")
    return re.sub(r"\s+", " ", text).strip()


def _url_reason(url: str) -> str:
    if not url:
        return "缺少链接"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "链接无法解析"
    if parsed.scheme not in {"http", "https"}:
        return "非网页链接"

    domain = _domain_of(url)
    if not domain:
        return "缺少域名"
    if _domain_blocked(domain):
        return "账号/百科/视频/应用商店页面"

    path = unquote(parsed.path or "").lower().rstrip("/")
    if re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z)$", path):
        return "文件/报告原文"
    if any(hint in path for hint in _NON_ARTICLE_PATH_HINTS):
        return "栏目页/登录页/列表页"
    tail = path.rsplit("/", 1)[-1]
    if tail in _NON_ARTICLE_SEGMENTS or not tail:
        return "站点入口页"
    # 拼音栏目名（如 /junshixinwen、/keji）也视为入口页
    if any(hint in tail for hint in _NON_ARTICLE_SLUG_HINTS):
        return "栏目入口页"
    # 单段纯字母路径（如 /politics、/kjdt）几乎都是栏目页
    if path.count("/") <= 1 and re.fullmatch(r"[a-z-]{2,24}", tail) and not re.search(r"\d", tail):
        return "栏目入口页"
    return ""


def is_probable_article(
    title: str | None,
    url: str | None,
    snippet: str | None = "",
    source: str | None = "",
) -> tuple[bool, str]:
    """返回 (是否像一篇新闻文章, 拒绝原因)。"""
    text = clean_title(title)
    raw_title = str(title or "").strip()

    if len(text) < 4:
        return False, "标题过短"
    if len(text) > 150:
        return False, "标题过长"

    normalized = _normalize_for_compare(text)
    if normalized in _GENERIC_TITLES_NORMALIZED:
        return False, "栏目名/导航标题"

    if _LOGIN_RE.search(raw_title) or _EN_PORTAL_RE.search(raw_title):
        return False, "登录/账户/应用商店页面"
    if _WIKI_RE.search(raw_title):
        return False, "百科词条"
    if _PROMO_RE.search(raw_title):
        return False, "推广内容"

    if _PORTAL_MARK_RE.search(text):
        return False, "门户栏目/直播聚合页"
    if _SITE_TAIL_RE.search(text):
        return False, "站点首页式标题"
    if _BROWSE_MARK_RE.search(text):
        return False, "列表/榜单/浏览页"
    if "百科" in text:
        return False, "百科词条"
    # 「频道名 + 分隔符 + 站点名」且没有事件动词 → 栏目/标签页
    if (
        _CATEGORY_WORD_RE.search(text)
        and _TITLE_SEPARATOR_RE.search(text)
        and not any(word in text for word in _EVENT_WORDS)
    ):
        return False, "栏目/站点标题"
    if text.endswith(("新闻", "新聞", "资讯", "資訊")) and len(text) <= 12:
        return False, "栏目标题"

    if text.count("|") + text.count("｜") >= 2:
        return False, "频道/导航式标题"
    if len(_LIST_MARK_RE.findall(text)) >= 2:
        return False, "列表式标题"

    snippet_text = str(snippet or "").strip()
    if len(_LIST_MARK_RE.findall(snippet_text)) >= 4:
        return False, "列表/导航式摘要"
    if snippet_text.startswith(("首页", "登录", "注册", "下载")):
        return False, "导航式摘要"

    reason = _url_reason(str(url or ""))
    if reason:
        return False, reason

    return True, ""


def is_article_title(title: str | None) -> bool:
    """只校验标题（用于模型改写后的最终标题）。"""
    text = clean_title(title)
    if len(text) < 6:
        return False
    if len(re.findall(r"[一-鿿]", text)) < 2:
        return False
    if _PORTAL_MARK_RE.search(text) or _LOGIN_RE.search(text) or _WIKI_RE.search(text):
        return False
    if _BROWSE_MARK_RE.search(text) or "百科" in text:
        return False
    if _normalize_for_compare(text) in _GENERIC_TITLES_NORMALIZED:
        return False
    if text.count("|") + text.count("｜") >= 2:
        return False
    if (
        _CATEGORY_WORD_RE.search(text)
        and _TITLE_SEPARATOR_RE.search(text)
        and not any(word in text for word in _EVENT_WORDS)
    ):
        return False
    return True
