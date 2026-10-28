from app.services.agent.runtime import _CitationStreamer


REFS = {
    "news:1": {"ref": "news:1", "kind": "news", "news_id": 1, "title": "A"},
    "news:2": {"ref": "news:2", "kind": "news", "news_id": 2, "title": "B"},
}


def _stream(text: str, size: int) -> str:
    streamer = _CitationStreamer(REFS)
    out = []
    for index in range(0, len(text), size):
        out.append(streamer.feed(text[index:index + size]))
    out.append(streamer.finish())
    return "".join(out)


def test_citation_split_across_chunks():
    text = "结论 [[news:1]]，补充 [[news:2]]。"
    for size in (1, 2, 3, 5, 7, 11):
        assert _stream(text, size) == "结论 [1]，补充 [2]。", f"chunk={size}"


def test_unknown_citation_is_dropped_while_streaming():
    result = _stream("虚构 [[news:99]] 结束", 4)
    assert "[[news" not in result
    assert result == "虚构  结束" or result.strip() == "虚构 结束"


def test_non_numeric_invalid_citation_is_dropped():
    # 模型偶尔会写成 [[news:自主]] 这类无效引用，应整体移除而不是原样展示
    result = _stream("结论 [[news:自主]] 结束", 3)
    assert "[[news" not in result
    assert "自主]]" not in result


def test_no_citation_text_passes_through():
    text = "普通文本，没有引用。"
    assert _stream(text, 2) == text
