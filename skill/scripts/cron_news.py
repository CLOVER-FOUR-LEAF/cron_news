#!/usr/bin/env python3
"""Cron News 辅助模式 CLI（仅标准库，无第三方依赖）。

用法示例：
  python3 cron_news.py --base http://localhost:18080 status
  python3 cron_news.py status
  python3 cron_news.py feed --days 7
  python3 cron_news.py check --url https://example.com/a --title "标题"
  python3 cron_news.py push --file news.json --batch 20261030T0800-a1b2 --check
  python3 cron_news.py brief --file brief.md --date 2026-10-30
  python3 cron_news.py onboard --file onboarding.json
  python3 cron_news.py memory --get
  python3 cron_news.py memory --patch patch.json
  python3 cron_news.py follow AI芯片
  python3 cron_news.py upload --file cover.png
  python3 cron_news.py feedback --status noted
  python3 cron_news.py ack 12
  python3 cron_news.py skill --path references/api.md

Base URL 优先级：--base > 环境变量 CRON_NEWS_BASE > http://localhost:18080
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_BASE = "http://localhost:18080"
TIMEOUT = 60
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def resolve_base(value: str | None) -> str:
    base = value or os.environ.get("CRON_NEWS_BASE") or DEFAULT_BASE
    return base.rstrip("/")


def api_request(
    base: str,
    method: str,
    path: str,
    *,
    body: Any = None,
    raw_body: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = TIMEOUT,
) -> Any:
    url = f"{base}{path}"
    data = raw_body
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif raw_body is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            if not payload:
                return None
            text = payload.decode("utf-8", errors="replace")
            if "application/json" in (response.headers.get("Content-Type") or ""):
                return json.loads(text)
            return text
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail") or detail
        except json.JSONDecodeError:
            pass
        raise ApiError(exc.code, str(detail)) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, f"无法连接 {url}：{exc.reason}") from exc


def get_json(base: str, path: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        clean = {key: value for key, value in params.items() if value is not None}
        path = f"{path}?{urllib.parse.urlencode(clean, doseq=True)}"
    return api_request(base, "GET", path)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def read_json(path: str) -> Any:
    text = read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 解析失败：{exc}") from exc


def validate_news(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    title = str(item.get("title") or "").strip()
    if not title:
        errors.append("title 必填")
    elif len(title) > 255:
        errors.append("title 超过 255 字")
    tags = item.get("tags")
    if tags is not None and not isinstance(tags, list):
        errors.append("tags 必须是数组")
    if item.get("credibility") is not None:
        try:
            score = int(item["credibility"])
            if not 0 <= score <= 100:
                errors.append("credibility 必须在 0-100")
        except (TypeError, ValueError):
            errors.append("credibility 必须是整数")
    for field, limit in (("source_url", 500), ("cover_url", 500), ("source", 100), ("batch_id", 40)):
        value = item.get(field)
        if value is not None and len(str(value)) > limit:
            errors.append(f"{field} 超过 {limit} 字")
    if item.get("analysis") is not None and not isinstance(item["analysis"], dict):
        errors.append("analysis 必须是对象")
    return errors


def normalize_push_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise SystemExit("push 文件必须是新闻对象、对象数组，或 {\"items\":[...]}")
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SystemExit(f"第 {index + 1} 条不是对象")
        result.append(item)
    return result


def cmd_health(args: argparse.Namespace) -> int:
    print_json(get_json(args.base, "/health"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print_json(get_json(args.base, "/api/assist/status"))
    return 0


def cmd_feed(args: argparse.Namespace) -> int:
    print_json(get_json(args.base, "/api/assist/feed", {"days": args.days, "limit": args.limit}))
    return 0


def cmd_news(args: argparse.Namespace) -> int:
    if args.id:
        print_json(get_json(args.base, f"/api/news/{args.id}"))
        return 0
    params = {
        "page": args.page,
        "page_size": args.page_size,
        "keyword": args.keyword,
        "tag": args.tag,
        "batch": args.batch,
        "today_only": "true" if args.today_only else None,
        "is_read": args.is_read,
    }
    print_json(get_json(args.base, "/api/news", params))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if not (args.url or args.title):
        raise SystemExit("--url 或 --title 至少提供一个")
    params = {"url": args.url, "title": args.title, "days": args.days}
    print_json(get_json(args.base, "/api/news/check", params))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    payload = read_json(args.file)
    items = normalize_push_items(payload)
    if args.batch:
        for item in items:
            if not item.get("batch_id"):
                item["batch_id"] = args.batch

    errors: list[str] = []
    for index, item in enumerate(items, start=1):
        problems = validate_news(item)
        if problems:
            errors.append(f"第 {index} 条：{'；'.join(problems)}")
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    pushed = skipped = failed = 0
    for index, item in enumerate(items, start=1):
        title = str(item.get("title") or "").strip()
        if args.check:
            check = get_json(
                args.base,
                "/api/news/check",
                {"url": item.get("source_url"), "title": title, "days": args.check_days},
            )
            if check.get("exists"):
                print(f"[{index}/{len(items)}] 跳过重复：{title}（{check.get('match')}，id={check.get('news_id')}）")
                skipped += 1
                continue
        if args.dry_run:
            print_json(item)
            continue
        try:
            result = api_request(args.base, "POST", "/api/news", body=item, timeout=args.timeout)
        except ApiError as exc:
            failed += 1
            print(f"[{index}/{len(items)}] 推送失败：{title} → {exc}", file=sys.stderr)
            if args.fail_fast:
                return 1
            continue
        pushed += 1
        print(
            f"[{index}/{len(items)}] 已推送 id={result.get('id')} batch={result.get('batch_id')} "
            f"cover={result.get('cover_url') or '—'} title={result.get('title')}"
        )
        if args.sleep:
            time.sleep(args.sleep)

    if not args.dry_run:
        print(f"完成：推送 {pushed}，跳过 {skipped}，失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_brief(args: argparse.Namespace) -> int:
    content = read_text(args.file)
    if not content.strip():
        raise SystemExit("简报内容为空")
    payload = {
        "tag": "每日",
        "date": args.date,
        "content": content,
        "source": args.source,
    }
    if args.note:
        payload["note"] = args.note
    result = api_request(args.base, "POST", "/api/brief", body=payload)
    print_json(result)
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    payload = read_json(args.file)
    if not isinstance(payload, dict):
        raise SystemExit("onboarding 文件必须是 JSON 对象")
    result = api_request(args.base, "POST", "/api/assist/onboarding", body=payload)
    print_json(result)
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    if args.get or not (args.patch or args.put):
        print_json(get_json(args.base, "/api/memory"))
        return 0
    if args.patch:
        payload = read_json(args.patch)
        if not isinstance(payload, dict) or "sections" not in payload:
            raise SystemExit("PATCH 文件应包含 {\"mode\":\"append\",\"sections\":{...}}")
        print_json(api_request(args.base, "PATCH", "/api/memory", body=payload))
        return 0
    content = read_text(args.put)
    print_json(api_request(args.base, "PUT", "/api/memory", body={"content": content}))
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    body = {"tag": args.tag, "follow": not args.unfollow}
    print_json(api_request(args.base, "POST", "/api/tags/follow", body=body))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    params = {"status": args.status, "limit": args.limit}
    print_json(get_json(args.base, "/api/feedback", params))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    print_json(api_request(args.base, "POST", f"/api/feedback/{args.id}/ack", body={}))
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    if args.manifest:
        print_json(get_json(args.base, "/api/skill/manifest"))
        return 0
    query = urllib.parse.urlencode({"path": args.path})
    text = api_request(args.base, "GET", f"/api/skill?{query}")
    if isinstance(text, (dict, list)):
        text = json.dumps(text, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(str(text), encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXT:
        raise SystemExit(f"仅支持 {'/'.join(sorted(ALLOWED_IMAGE_EXT))}")
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise SystemExit("图片超过 8MB")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    boundary = f"----cronnews{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    result = api_request(
        args.base,
        "POST",
        "/api/media",
        raw_body=b"".join(parts),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    print_json(result)
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    print_json(api_request(args.base, "POST", "/api/scheduler/trigger", body={}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cron News 辅助模式命令行工具（仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base", default=None, help="平台地址，默认 $CRON_NEWS_BASE 或 http://localhost:18080")
    parser.add_argument("--timeout", type=int, default=TIMEOUT, help="请求超时秒数")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="GET /health").set_defaults(func=cmd_health)
    sub.add_parser("status", help="GET /api/assist/status").set_defaults(func=cmd_status)

    p = sub.add_parser("feed", help="GET /api/assist/feed")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=300)
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("news", help="查询新闻列表或详情")
    p.add_argument("--id", type=int)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page-size", type=int, default=20, dest="page_size")
    p.add_argument("--keyword")
    p.add_argument("--tag")
    p.add_argument("--batch")
    p.add_argument("--today-only", action="store_true", dest="today_only")
    p.add_argument("--is-read", type=int, dest="is_read")
    p.set_defaults(func=cmd_news)

    p = sub.add_parser("check", help="推送前查重")
    p.add_argument("--url")
    p.add_argument("--title")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("push", help="推送一条或多条新闻")
    p.add_argument("--file", required=True, help="JSON 文件，- 表示 stdin")
    p.add_argument("--batch", help="为本轮注入 batch_id（仅对缺失的条目）")
    p.add_argument("--check", action="store_true", help="推送前调用 /api/news/check 跳过重复")
    p.add_argument("--check-days", type=int, default=30, dest="check_days")
    p.add_argument("--sleep", type=float, default=0.2, help="每条之间的等待秒数")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument(
        "--fail-fast",
        action="store_true",
        dest="fail_fast",
        help="遇到第一条失败就中止；默认继续推送并汇总失败",
    )
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("brief", help="写入每日简报")
    p.add_argument("--file", required=True, help="Markdown 文件，- 表示 stdin")
    p.add_argument("--date", required=True)
    p.add_argument("--note")
    p.add_argument("--source", default="外部")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("onboard", help="提交首次访谈结果")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("memory", help="读取 / 更新记忆")
    p.add_argument("--get", action="store_true")
    p.add_argument("--patch", help="PATCH JSON 文件")
    p.add_argument("--put", help="PUT 整篇 Markdown 文件")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("follow", help="关注标签")
    p.add_argument("tag")
    p.add_argument("--unfollow", action="store_true")
    p.set_defaults(func=cmd_follow)

    p = sub.add_parser("feedback", help="读取反馈")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("ack", help="标记反馈已处理")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("skill", help="读取 Skill 文件")
    p.add_argument("--path", default="SKILL.md")
    p.add_argument("--manifest", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("upload", help="上传封面图")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_upload)

    sub.add_parser("trigger", help="触发内置采集（辅助模式会 409）").set_defaults(func=cmd_trigger)
    return parser


def _reorder_global_args(argv: list[str]) -> list[str]:
    """允许 --base/--timeout 放在子命令后面（argparse 默认只认子命令前）。"""
    head: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--base", "--timeout") and index + 1 < len(argv):
            head.extend([token, argv[index + 1]])
            index += 2
            continue
        if token.startswith("--base=") or token.startswith("--timeout="):
            head.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return head + rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_reorder_global_args(raw))
    args.base = resolve_base(args.base)
    try:
        return int(args.func(args) or 0)
    except ApiError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
