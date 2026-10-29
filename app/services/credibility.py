"""来源可信度评估。

这是「信源可靠性」而不是「事实真伪」的启发式评分，用于采集时过滤明显低质内容，
并在详情页向用户透明展示。评分会写入 news.credibility 与结构化分析。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

# 权威/一手信源
HIGH_TRUST = {
    "gov.cn", "xinhuanet.com", "news.cn", "people.com.cn", "cctv.com", "chinanews.com.cn",
    "thepaper.cn", "caixin.com", "yicai.com", "21jingji.com", "stcn.com", "cls.cn",
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com", "nytimes.com",
    "bbc.com", "bbc.co.uk", "economist.com", "nature.com", "science.org", "ieee.org",
    "who.int", "un.org", "nato.int", "imf.org", "worldbank.org",
}
# 主流媒体 / 垂直媒体
GOOD_TRUST = {
    "36kr.com", "huxiu.com", "jiqizhixin.com", "qbitai.com", "leiphone.com", "geekpark.net",
    "ithome.com", "cnbeta.com", "techcrunch.com", "theverge.com", "arstechnica.com",
    "wired.com", "engadget.com", "zdnet.com", "cnet.com", "venturebeat.com",
    "sina.com.cn", "163.com", "qq.com", "sohu.com", "ifeng.com", "guancha.cn",
    "bjnews.com.cn", "beijingnews.com.cn", "nbd.com.cn", "eeo.com.cn", "infzm.com",
}
# 聚合/自媒体/社交平台：可参考，但需要交叉验证
AGGREGATOR = {
    "baijiahao.baidu.com", "zhihu.com", "weibo.com", "toutiao.com", "douyin.com",
    "bilibili.com", "jianshu.com", "medium.com", "substack.com", "reddit.com",
    "x.com", "twitter.com", "facebook.com", "youtube.com", "mp.weixin.qq.com",
    "so.com", "hao123.com", "wenku.baidu.com", "docin.com",
}

CLICKBAIT = (
    "震惊", "惊天", "竟然", "内幕", "揭秘", "必看", "速看", "太可怕", "炸裂",
    "沸腾", "彻底慌了", "刚刚", "重磅！", "独家爆料", "不看后悔", "全网疯传",
)
LOW_VALUE = ("招聘", "优惠券", "折扣", "直播预告", "图集", "视频合集", "下载 app", "下载APP")


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _match(domain: str, values: set[str]) -> bool:
    if not domain:
        return False
    return any(domain == value or domain.endswith("." + value) for value in values)


def _base_score(domain: str) -> tuple[int, str]:
    if _match(domain, HIGH_TRUST):
        return 88, "权威/一手信源"
    if _match(domain, GOOD_TRUST):
        return 76, "主流或垂直媒体"
    if _match(domain, AGGREGATOR):
        return 52, "聚合/自媒体平台"
    if not domain:
        return 45, "缺少来源域名"
    if domain.endswith(".gov.cn") or domain.endswith(".edu.cn") or domain.endswith(".edu"):
        return 85, "政府/教育机构域名"
    if any(token in domain for token in ("blog", "weibo", "tieba", "bbs", "forum")):
        return 42, "博客/论坛"
    return 58, "普通站点"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (ValueError, TypeError):
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(raw[:19], pattern)
        except (ValueError, TypeError):
            continue
    return None


def assess(
    hit: dict[str, Any],
    *,
    fetch: dict[str, Any] | None = None,
    corroboration: int = 0,
    source_name: str = "",
) -> dict[str, Any]:
    """返回 {score, label, signals, domain}。"""
    url = str(hit.get("url") or hit.get("link") or "")
    domain = domain_of(url)
    score, domain_reason = _base_score(domain)
    signals: list[str] = [domain_reason]

    if url.startswith("https://"):
        score += 3
    elif url.startswith("http://"):
        score -= 3
        signals.append("非 HTTPS")

    title = str(hit.get("title") or "").strip()
    if 8 <= len(title) <= 90:
        score += 3
    else:
        score -= 4
        signals.append("标题长度异常")
    if any(token in title for token in CLICKBAIT):
        score -= 14
        signals.append("标题党")
    if any(token in title for token in LOW_VALUE):
        score -= 12
        signals.append("低价值内容")
    if title.count("!") + title.count("！") + title.count("?") + title.count("？") >= 4:
        score -= 4
        signals.append("标点夸张")

    snippet = str(hit.get("snippet") or hit.get("description") or "").strip()
    if len(snippet) >= 100:
        score += 4
    elif len(snippet) >= 50:
        score += 2
    elif len(snippet) < 20:
        score -= 4
        signals.append("摘要过短")

    published = parse_date(str(hit.get("published_date") or hit.get("date") or ""))
    if published:
        age = datetime.now() - published
        if age < timedelta(days=3):
            score += 5
        elif age < timedelta(days=14):
            score += 2
        elif age > timedelta(days=60):
            score -= 12
            signals.append("内容陈旧")
    else:
        score -= 2
        signals.append("缺少发布时间")

    if fetch:
        if fetch.get("ok"):
            score += 9
            signals.append("已抓取原文")
        else:
            score -= 6
            signals.append("原文抓取失败")

    if corroboration >= 2:
        score += 9
        signals.append(f"{corroboration} 个来源交叉出现")
    elif corroboration == 1:
        score += 3

    if source_name and domain and source_name.lower().replace("www.", "") in domain:
        score += 2

    if not str(hit.get("source") or hit.get("domain") or "").strip() and not domain:
        score -= 5
        signals.append("无来源标识")

    score = max(0, min(100, int(round(score))))
    return {
        "score": score,
        "label": "高" if score >= 75 else ("中" if score >= 55 else "低"),
        "signals": signals[:8],
        "domain": domain,
    }


def min_score() -> int:
    from app.env_store import read_env_file

    try:
        return max(0, min(100, int(read_env_file().get("AGENT_MIN_CREDIBILITY", "40") or 40)))
    except (TypeError, ValueError):
        return 40


def title_looks_low_quality(title: str) -> bool:
    title = (title or "").strip()
    if len(title) < 4:
        return True
    if any(token in title for token in LOW_VALUE):
        return True
    if re.fullmatch(r"[\W_]+", title):
        return True
    return False
