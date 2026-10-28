from app.services.agent.runtime import finalize_citations


def test_citations_are_rewritten_and_numbered():
    refs = {
        "news:1": {"ref": "news:1", "kind": "news", "news_id": 1, "title": "A", "source_url": "https://a"},
        "news:2": {"ref": "news:2", "kind": "news", "news_id": 2, "title": "B", "source_url": "https://b"},
    }
    content, citations = finalize_citations("结论 [[news:2]]，补充 [[news:1]]，再提一次 [[news:2]]。", refs)
    assert content == "结论 [1]，补充 [2]，再提一次 [1]。"
    assert [item["news_id"] for item in citations] == [2, 1]
    assert [item["n"] for item in citations] == [1, 2]


def test_unknown_citation_is_removed():
    content, citations = finalize_citations("虚构来源 [[news:99]]。", {})
    assert "[[news:99]]" not in content
    assert citations == []


def test_web_citation_supported():
    refs = {"web:abc123": {"ref": "web:abc123", "kind": "web", "title": "W", "source_url": "https://w"}}
    content, citations = finalize_citations("来自网络 [[web:abc123]]。", refs)
    assert content == "来自网络 [1]。"
    assert citations[0]["kind"] == "web"


def test_single_bracket_citation_supported():
    refs = {"news:7": {"ref": "news:7", "kind": "news", "news_id": 7, "title": "G"}}
    content, citations = finalize_citations("单括号 [news:7]。", refs)
    assert content == "单括号 [1]。"
    assert citations[0]["news_id"] == 7
