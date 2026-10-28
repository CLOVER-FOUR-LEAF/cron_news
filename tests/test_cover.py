from app.services.cover_service import (
    COVER_MODE_IMAGE,
    COVER_MODE_OFF,
    COVER_MODE_SVG,
    MODE_LABELS,
    cover_mode,
)
from app.services.svg_cover import sanitize_svg


EVIL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900" onload="alert(1)">
  <script>alert(1)</script>
  <a href="javascript:alert(2)"><rect width="10" height="10"/></a>
  <image href="https://evil.example/x.png"/>
  <foreignObject><div>x</div></foreignObject>
  <rect x="0" y="0" width="100" height="100" fill="#123456"/>
  <circle cx="50" cy="50" r="20" style="fill: url(javascript:alert(3))"/>
  <path d="M0 0 L10 10" style="fill:url(#grad)"/>
</svg>"""


def test_sanitize_removes_scripts_and_external_refs():
    svg = sanitize_svg(EVIL_SVG)
    assert svg is not None
    lowered = svg.lower()
    for token in ("<script", "javascript:", "<foreignobject", "<image", "onload"):
        assert token not in lowered


def test_sanitize_keeps_safe_shapes_and_style_refs():
    svg = sanitize_svg(EVIL_SVG)
    assert "<rect" in svg and "<path" in svg
    assert "url(#grad)" in svg
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


def test_sanitize_strips_markdown_fence():
    raw = "```svg\n<svg viewBox='0 0 10 10'><circle cx='1' cy='1' r='1'/></svg>\n```"
    svg = sanitize_svg(raw)
    assert svg is not None and svg.startswith("<svg")
    assert "```" not in svg


def test_sanitize_rejects_non_svg_and_garbage():
    assert sanitize_svg("这里没有 SVG") is None
    assert sanitize_svg("<svg><unclosed>") is None
    assert sanitize_svg("") is None


def test_sanitize_fills_missing_viewbox():
    svg = sanitize_svg("<svg><rect width='5' height='5'/></svg>")
    assert svg is not None
    assert 'viewBox="0 0 1600 900"' in svg


def test_cover_mode_defaults_to_off_without_config(tmp_path, monkeypatch):
    import app.services.cover_service as cover

    monkeypatch.setattr(cover, "read_env_file", lambda: {})
    assert cover_mode() == COVER_MODE_OFF

    monkeypatch.setattr(cover, "read_env_file", lambda: {"AGENT_COVER_ENABLED": "true"})
    assert cover_mode() == COVER_MODE_IMAGE

    monkeypatch.setattr(cover, "read_env_file", lambda: {"AGENT_COVER_MODE": "svg"})
    assert cover_mode() == COVER_MODE_SVG

    monkeypatch.setattr(cover, "read_env_file", lambda: {"AGENT_COVER_MODE": "bogus"})
    assert cover_mode() == COVER_MODE_OFF


def test_mode_labels_cover_all_modes():
    assert set(MODE_LABELS) == {COVER_MODE_IMAGE, COVER_MODE_SVG, COVER_MODE_OFF}
