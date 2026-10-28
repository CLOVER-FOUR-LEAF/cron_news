from starlette.requests import Request

from app.base_path import (
    BasePathRewriteMiddleware,
    normalize_base_path,
    rewrite_json_value,
    rewrite_text,
)

BASE = "/proxy/8000"


def _request(host: str, referer: str | None = None, scheme: str = "http") -> Request:
    headers = [(b"host", host.encode())]
    if referer:
        headers.append((b"referer", referer.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": "/chat",
        "raw_path": b"/chat",
        "query_string": b"",
        "headers": headers,
        "client": ("1.2.3.4", 1234),
        "server": ("0.0.0.0", 8000),
    }
    return Request(scope)


def test_base_path_is_detected_only_for_proxied_requests():
    middleware = BasePathRewriteMiddleware(None, BASE)
    assert middleware._base_for_request(_request("192.168.144.175:8000")) == ""
    assert middleware._base_for_request(_request("192.168.144.175:8443")) == BASE
    assert (
        middleware._base_for_request(
            _request("127.0.0.1:8000", referer="https://host:8443/proxy/8000/chat")
        )
        == BASE
    )


def test_normalize_base_path():
    assert normalize_base_path("proxy/8000/") == "/proxy/8000"
    assert normalize_base_path("/proxy/8000") == "/proxy/8000"
    assert normalize_base_path("") == ""


def test_html_links_and_assets_are_prefixed():
    html = (
        '<link rel="stylesheet" href="/static/css/style.css?v=41">'
        '<script src="/static/js/chat.js"></script>'
        '<a href="/chat">助手</a>'
        '<img src="/images/sign.png">'
    )
    result = rewrite_text(html, BASE)
    assert 'href="/proxy/8000/static/css/style.css?v=41"' in result
    assert 'src="/proxy/8000/static/js/chat.js"' in result
    assert 'href="/proxy/8000/chat"' in result
    assert 'src="/proxy/8000/images/sign.png"' in result


def test_inline_js_paths_are_prefixed():
    html = (
        "fetch('/api/news');"
        "fetch(`/api/news/${id}`);"
        "new EventSource('/api/scheduler/trigger/stream');"
        "window.location.href = `/news/${item.id}`;"
        "url: '/openapi.json';"
    )
    result = rewrite_text(html, BASE)
    assert "fetch('/proxy/8000/api/news')" in result
    assert "fetch(`/proxy/8000/api/news/${id}`)" in result
    assert "EventSource('/proxy/8000/api/scheduler/trigger/stream')" in result
    assert "window.location.href = `/proxy/8000/news/${item.id}`" in result
    assert "url: '/proxy/8000/openapi.json'" in result


def test_external_and_already_prefixed_urls_are_untouched():
    html = (
        '<a href="https://example.com/x">x</a>'
        '<script src="//cdn.example.com/x.js"></script>'
        '<a href="/proxy/8000/chat">already</a>'
    )
    assert rewrite_text(html, BASE) == html


def test_css_url_is_prefixed():
    css = "background: url(/images/a.png); mask: url('/images/b.svg');"
    result = rewrite_text(css, BASE)
    assert "url(/proxy/8000/images/a.png)" in result
    assert "url('/proxy/8000/images/b.svg')" in result


def test_json_local_paths_are_prefixed():
    data = {
        "items": [
            {"cover_url": "/images/cover/1.png", "source_url": "https://example.com/a"},
            {"cover_url": "/proxy/8000/images/cover/2.png"},
        ]
    }
    result = rewrite_json_value(data, BASE)
    assert result["items"][0]["cover_url"] == "/proxy/8000/images/cover/1.png"
    assert result["items"][0]["source_url"] == "https://example.com/a"
    assert result["items"][1]["cover_url"] == "/proxy/8000/images/cover/2.png"
