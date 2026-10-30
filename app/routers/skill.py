"""对外分发外部 Agent 接入 Skill：manifest + 单文件读取。

Skill 文件位于项目 `skill/` 目录。外部 Agent 可以先拉取 `/api/skill/manifest`，
再按需读取 `/api/skill?path=references/api.md`。只允许读取 skill 目录内的文本文件，
并做路径穿越与大小校验。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.config import BASE_DIR
from app.services.assist_service import SKILL_VERSION

router = APIRouter(prefix="/api/skill", tags=["skill"])

SKILL_DIR = (BASE_DIR / "skill").resolve()
ALLOWED_SUFFIXES = {".md", ".json", ".txt", ".py", ".yaml", ".yml"}
MAX_FILE_BYTES = 512 * 1024
MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".yaml": "text/yaml; charset=utf-8",
    ".yml": "text/yaml; charset=utf-8",
}


def _resolve_skill_file(relative: str) -> Path:
    raw = (relative or "SKILL.md").strip().lstrip("/")
    if not raw:
        raw = "SKILL.md"
    candidate = (SKILL_DIR / raw).resolve()
    try:
        candidate.relative_to(SKILL_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法的 Skill 路径") from exc
    if candidate.suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="不支持的 Skill 文件类型")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Skill 文件不存在")
    if candidate.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Skill 文件过大")
    return candidate


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value:
            data[key] = value
    return data


def _skill_files() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if not SKILL_DIR.is_dir():
        return items
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.name.startswith("."):
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        try:
            relative = path.relative_to(SKILL_DIR).as_posix()
            size = path.stat().st_size
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        items.append(
            {
                "path": relative,
                "size": size,
                "sha256": digest,
                "url": f"/api/skill?path={relative}",
            }
        )
    return items


@router.get("/manifest")
async def skill_manifest():
    files = _skill_files()
    skill_md = next((item for item in files if item["path"] == "SKILL.md"), None)
    meta: dict[str, str] = {}
    if skill_md:
        try:
            meta = _frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
        except OSError:
            meta = {}
    return {
        "name": meta.get("name") or "cron-news",
        "version": meta.get("version") or SKILL_VERSION,
        "description": meta.get("description") or "定时资讯辅助模式外部 Agent 接入 Skill",
        "entry": "/api/skill",
        "files": files,
    }


@router.get("")
async def skill_file(path: str = Query("SKILL.md", max_length=300)):
    candidate = _resolve_skill_file(path)
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取 Skill 失败：{exc}") from exc
    media_type = MEDIA_TYPES.get(candidate.suffix.lower(), "text/plain; charset=utf-8")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Skill-Name": "cron-news",
            "X-Skill-Version": SKILL_VERSION,
            "Cache-Control": "no-cache",
        },
    )
