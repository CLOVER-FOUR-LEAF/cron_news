"""采集质量：去重、可信度、原文抽取与个性化打分。"""

from collections import Counter
from datetime import date, datetime, timedelta

from app.models import News
from app.services import article_fetch, brief_service, content_quality, credibility, dedup, recommend_service
from app.services.json_utils import extract_json_object


def _news(*, tags=None, source="测试来源", title="英伟达发布新一代 AI 芯片"):
    return News(
        title=title,
        summary="摘要",
        source=source,
        collected_at=datetime.now(),
        tags='["AI芯片", "半导体"]' if tags is None else tags,
        credibility=80,
    )


def test_url_and_title_normalization_dedup():
    assert dedup.normalize_url("https://www.example.com/a/?utm_source=x&id=1#top") == dedup.normalize_url(
        "http://example.com/a?id=1"
    )
    assert dedup.title_similarity(
        "英伟达发布新一代 AI 芯片 - 科技日报",
        "英伟达发布新一代AI芯片",
    ) >= 0.82
    assert dedup.title_similarity("英伟达发布新一代 AI 芯片", "央行下调存款准备金率") < 0.5


def test_credibility_prefers_authoritative_source():
    good = credibility.assess(
        {
            "title": "美联储宣布维持利率不变",
            "url": "https://www.reuters.com/markets/rates-123",
            "snippet": "美联储周三宣布维持基准利率不变，并给出了对通胀的最新判断。" * 2,
            "source": "Reuters",
            "published_date": datetime.now().isoformat(),
        },
        fetch={"ok": True},
        corroboration=2,
    )
    clickbait = credibility.assess(
        {
            "title": "震惊！竟然发生了这种事，全网疯传！！",
            "url": "https://baijiahao.baidu.com/s?id=1",
            "snippet": "短",
            "source": "百家号",
        },
        fetch={"ok": False, "reason": "http_403"},
    )
    assert good["score"] >= 75
    assert good["score"] > clickbait["score"]
    assert credibility.title_looks_low_quality("招聘") is True


def test_extract_article_text_from_html():
    html = """
    <html><head><title>测试标题</title>
    <meta property="article:published_time" content="2026-10-28T09:00:00+08:00">
    </head><body>
    <nav>首页 登录 注册</nav>
    <article>
      <h1>英伟达发布新一代芯片</h1>
      <p>英伟达在发布会上公布了新一代 AI 芯片，面向推理场景，能效明显提升。</p>
      <p>公司表示，新芯片将在明年一季度开始出货，合作伙伴包括多家云服务厂商。</p>
      <p>分析师认为，这将进一步加剧数据中心芯片市场的竞争。</p>
    </article>
    <footer>版权所有</footer>
    </body></html>
    """
    result = article_fetch.extract_article_text(html)
    assert result["ok"] is True
    assert "英伟达" in result["text"]
    assert "首页" not in result["text"]
    assert result["published_date"].startswith("2026-10-28")


def test_extract_json_object_survives_model_chatter():
    assert extract_json_object('好的，结果如下：{"topics":["AI"]} 以上。') == {"topics": ["AI"]}
    assert extract_json_object('{not json} {"answer":"ok"}') == {"answer": "ok"}
    assert extract_json_object("没有 JSON") is None


def test_personalized_score_penalizes_negative_feedback():
    news = _news(source="低质来源")
    now = datetime.now()
    profile = {"has_signal": True}
    recent = {"ids": set(), "related_counts": Counter(), "related_reason": {}, "tags": Counter()}
    base_args = dict(
        tag_map={"AI芯片": 1.0, "半导体": 0.6},
        source_map={"低质来源": 0.8},
        recent=recent,
        now=now,
    )
    neutral = {
        "ids": set(),
        "sources": {},
        "tags": {},
        "reasons": {},
        "positive": {},
    }
    negative = {
        "ids": set(),
        "sources": {"低质来源": 1},
        "tags": {"AI芯片": 1},
        "reasons": {"source": 1},
        "positive": {},
    }
    score_ok = recommend_service._score_candidate(news, negative=neutral, **base_args)[0]
    score_bad = recommend_service._score_candidate(news, negative=negative, **base_args)[0]
    assert score_bad < score_ok


def test_recency_decay_and_reason():
    now = datetime.now()
    fresh = _news(title="刚刚发布的新芯片")
    fresh.collected_at = now - timedelta(hours=1)
    old = _news(title="很久以前的芯片新闻")
    old.collected_at = now - timedelta(days=10)
    assert recommend_service._recency_score(fresh.collected_at, now) > recommend_service._recency_score(
        old.collected_at, now
    )
    reason = recommend_service._reason_for(
        fresh,
        tag_affinity=0.9,
        top_tag="AI芯片",
        matched_topic="半导体",
        related_title=None,
        source_affinity=0.8,
        hours=1,
    )
    assert "AI芯片" in reason and "半导体" in reason


def test_content_quality_blocks_portals_logins_and_nav_pages():
    """搜索结果里的门户首页、登录页、栏目页、百科/视频页不能当成新闻。"""
    junk = [
        ("Sign in to your account", "https://myaccount.microsoft.com"),
        ("Outlook", "https://outlook.office.com/mail"),
        ("Microsoft - Wikipedia", "https://en.wikipedia.org/wiki/Microsoft"),
        ("Microsoft – AI, Cloud, Productivity, Computing, Gaming & Apps", "https://www.microsoft.com"),
        ("财经首页 - 东方财富网", "https://finance.eastmoney.com"),
        ("7×24小时全球实时财经新闻直播_直播_财经_新浪网", "https://tzxy.sina.com.cn/kuaixun"),
        ("国际财经 | 财经 | 世界新闻网", "https://www.worldjournal.com/wj/cate/business/121209"),
        ("军事 / 军事新闻-中華新聞网 中華新聞社", "https://www.chinanews.org/junshi/junshixinwen"),
        ("财经头条、财经新闻、最新资讯 - FastBull", "https://www.fastbull.com/cn/news"),
        ("科技创新", "https://www.dw.com/zh/%E7%A7%91%E6%8A%80%E5%88%9B%E6%96%B0/s-1686"),
        ("RIMPAC News — U.S. Pacific Fleet", "https://www.cpf.navy.mil/rimpac/news/videoid/1017011"),
    ]
    for title, url in junk:
        ok, reason = content_quality.is_probable_article(title, url, "首页 · 新闻 · 财经 · 科技")
        assert not ok, f"应拒绝：{title}（{reason}）"


def test_content_quality_accepts_real_article_links():
    good = [
        ("英伟达发布新一代 AI 芯片，推理能效提升 40%", "https://www.reuters.com/technology/nvidia-unveils-new-ai-chip-2026-10-29/"),
        ("央行宣布下调存款准备金率 0.25 个百分点", "https://finance.sina.com.cn/money/bank/2026-10-29/doc-inhxxx.shtml"),
        ("OpenAI 推出新一代推理模型，API 价格下调一半", "https://www.jiqizhixin.com/articles/2026-10-29-3"),
        ("美联储维持利率不变，暗示年内或再降息一次", "https://www.bloomberg.com/news/articles/2026-10-29/fed-holds-rates-steady"),
    ]
    snippet = "这是一段正常的新闻摘要，包含事件主体与关键事实。" * 3
    for title, url in good:
        ok, reason = content_quality.is_probable_article(title, url, snippet)
        assert ok, f"应保留：{title}（{reason}）"


def test_clean_title_strips_site_suffix_and_promo_prefix():
    assert content_quality.clean_title("[Featured] 英伟达发布新芯片 - 36氪") == "英伟达发布新芯片"
    assert content_quality.clean_title("美联储维持利率不变_新浪财经") == "美联储维持利率不变"


def test_article_title_requires_chinese_and_no_portal_markers():
    assert content_quality.is_article_title("英伟达发布新一代 AI 芯片")
    assert not content_quality.is_article_title("NVIDIA unveils new AI chip")
    assert not content_quality.is_article_title("科技新闻首页")
    assert not content_quality.is_article_title("Sign in to your account")


def test_brief_detects_provider_filter_and_builds_fallback():
    assert brief_service._blocked_by_filter("", "content_filter")
    assert brief_service._blocked_by_filter(
        "The request was rejected because it was considered high risk", "stop"
    )
    assert not brief_service._blocked_by_filter("# 每日简报", "stop")

    content = brief_service._fallback_brief([_news()], date(2026, 10, 29))
    assert "# 每日简报 · 2026-10-29" in content
    assert "英伟达发布新一代 AI 芯片" in content
    assert "## 今日要闻" in content
