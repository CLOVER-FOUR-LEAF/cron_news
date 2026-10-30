"""反向代理子路径支持（code-server / VS Code 端口转发）。

code-server 的 `/proxy/<port>/` 会把 `/proxy/8000/chat` 转发给应用时去掉前缀，
但**不会重写响应里的根路径 URL**。因此页面能打开，`/static/...`、`/images/...`、
`/api/...` 却会请求到 code-server 根路径而 404，表现就是「只有 HTML 布局」。

设置 `APP_BASE_PATH=/proxy/8000` 后，本中间件会把 HTML/JS/CSS/JSON 中的根路径
改写成带前缀的路径。不设置时完全不影响现有部署。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.env_store import read_env_file
from app.logging_setup import get_logger

logger = get_logger("base_path")

TEXT_CONTENT_TYPES = (
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
)

# 这些 URL 一定指向本应用的静态资源 / 接口，JSON 响应里也值得改写
LOCAL_PREFIXES = ("/images/", "/static/", "/api/")


def normalize_base_path(value: str) -> str:
    base = (value or "").strip()
    if not base:
        return ""
    if not base.startswith("/"):
        base = "/" + base
    return base.rstrip("/")


def _lookahead(base: str) -> str:
    """匹配单个根路径斜杠：排除协议相对 URL，并避免对已带前缀的 URL 二次改写。"""
    stripped = base.lstrip("/")
    if stripped:
        return rf"/(?!/)(?!(?:{re.escape(stripped)})/)"
    return r"/(?!/)"


def rewrite_text(text: str, base: str) -> str:
    if not base:
        return text

    lookahead = _lookahead(base)
    context = re.compile(
        r"(?P<ctx>fetch\(\s*|EventSource\(\s*|location\.href\s*=\s*|"
        r"location\.assign\(\s*|location\.replace\(\s*|window\.location\.href\s*=\s*|"
        r"window\.location\s*=\s*|window\.open\(\s*|href\s*=\s*|src\s*=\s*|"
        r"action\s*=\s*|poster\s*=\s*|url:\s*)"
        r"(?P<quote>[\"'`])"
        f"{lookahead}"
    )
    text = context.sub(
        lambda match: f"{match.group('ctx')}{match.group('quote')}{base}/",
        text,
    )

    # 模板字符串直接以 / 开头（例如 `location.href = `/news/${id}``）
    text = re.sub(r"`" + lookahead, "`" + base + "/", text)

    # CSS url(/...)
    text = re.sub(
        rf"url\((?P<quote>[\"']?){lookahead}",
        lambda match: f"url({match.group('quote')}{base}/",
        text,
    )
    return text


def rewrite_json_value(value: Any, base: str) -> Any:
    if isinstance(value, dict):
        return {key: rewrite_json_value(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_json_value(item, base) for item in value]
    if isinstance(value, str):
        if value.startswith(base + "/"):
            return value
        if value.startswith(LOCAL_PREFIXES):
            return base + value
    return value


class BasePathRewriteMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, base_path: str):
        super().__init__(app)
        self.base_path = normalize_base_path(base_path)

    def _base_for_request(self, request) -> str:
        """同一次部署同时支持「CodeServer 子路径」和「局域网直连」。

        - Referer 里带 /proxy/<port>/ 时直接采用该前缀；
        - 否则比较请求 Host 的端口与 uvicorn 实际监听端口：不同即视为经代理访问；
        - 端口相同则视为直连，不做任何改写。
        """
        if not self.base_path:
            return ""
        if os.environ.get("APP_BASE_PATH_ALWAYS") == "1":
            return self.base_path

        referer = request.headers.get("referer") or ""
        match = re.search(r"(/proxy/\d+)/", referer)
        if match:
            return normalize_base_path(match.group(1))

        server = request.scope.get("server") or ()
        server_port = server[1] if len(server) > 1 else None
        try:
            request_port = request.url.port
        except Exception:
            request_port = None
        if request_port is None:
            request_port = 443 if request.url.scheme == "https" else 80
        if server_port and int(request_port) != int(server_port):
            return self.base_path
        return ""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        base = self._base_for_request(request)
        if not base or request.method == "HEAD":
            return response
        if response.status_code in (204, 206, 304) or response.headers.get("content-range"):
            return response
        if response.headers.get("content-disposition"):
            return response
        if response.headers.get("content-encoding") not in (None, "", "identity"):
            return response

        content_type = (response.headers.get("content-type") or "").lower()
        is_json = "application/json" in content_type
        is_text = any(item in content_type for item in TEXT_CONTENT_TYPES)
        if not (is_json or is_text):
            return response
        if not hasattr(response, "body_iterator"):
            return response

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
        body = b"".join(chunks)

        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
            )

        if is_json:
            try:
                text = json.dumps(
                    rewrite_json_value(json.loads(text), base),
                    ensure_ascii=False,
                )
            except (json.JSONDecodeError, TypeError):
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )
        else:
            text = rewrite_text(text, base)

        data = text.encode("utf-8")
        headers = MutableHeaders(response.headers)
        headers["content-length"] = str(len(data))
        headers.pop("etag", None)
        headers.pop("last-modified", None)
        return Response(
            content=data,
            status_code=response.status_code,
            headers=dict(headers),
        )


def install_base_path_middleware(app) -> str:
    base = normalize_base_path(
        read_env_file().get("APP_BASE_PATH", "") or os.environ.get("APP_BASE_PATH", "")
    )
    if not base:
        return ""
    app.add_middleware(BasePathRewriteMiddleware, base_path=base)
    logger.info("已启用反向代理路径前缀：%s", base)
    return base
