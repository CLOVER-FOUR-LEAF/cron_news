"""图片上传：供外部 Agent 远程上传封面，再随新闻推送交给平台改名归档。

流程：
1. 外部 Agent `POST /api/media` 上传图片，得到 `/images/uploads/<uuid>.<ext>`；
2. 推送新闻时把该地址作为 `cover_url`；
3. 平台在 `POST /api/news` 中把图片复制归档到 `/images/cover/{新闻id}.<ext>`（源文件保留）。

只接受常见位图格式，并校验文件头，避免把 HTML/脚本伪装成图片。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import BASE_DIR

router = APIRouter(prefix="/api/media", tags=["media"])

UPLOAD_DIR = BASE_DIR / "images" / "uploads"
MAX_BYTES = 8 * 1024 * 1024

# content_type -> (扩展名, 允许的文件头)
_IMAGE_TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/jpg": (".jpg", (b"\xff\xd8\xff",)),
    "image/gif": (".gif", (b"GIF87a", b"GIF89a")),
}
_EXT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _is_webp(data: bytes) -> bool:
    return (
        len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    )


def _detect(data: bytes, content_type: str, filename: str) -> tuple[str, str] | None:
    """返回 (扩展名, 规范化 content_type)，不匹配时返回 None。"""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()

    if ctype in ("image/webp",) or suffix == ".webp":
        return (".webp", "image/webp") if _is_webp(data) else None

    if ctype in _IMAGE_TYPES:
        suffix, magic = _IMAGE_TYPES[ctype]
        return (suffix, ctype) if data.startswith(magic) else None

    # content_type 缺失或不可信时，按扩展名 + 文件头兜底
    ext = ".jpg" if suffix == ".jpeg" else suffix
    if ext == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if ext == ".jpg" and data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if ext == ".gif" and data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    return None


@router.post("")
async def upload_media(file: UploadFile = File(...)):
    data = await file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="图片过大（上限 8MB）")

    detected = _detect(data, file.content_type or "", file.filename or "")
    if not detected:
        raise HTTPException(
            status_code=400,
            detail="仅支持 PNG / JPEG / GIF / WebP 图片，且文件内容需与类型一致",
        )
    suffix, content_type = detected

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{suffix}"
    target = UPLOAD_DIR / name
    await asyncio.to_thread(target.write_bytes, data)

    return {
        "ok": True,
        "url": f"/images/uploads/{name}",
        "filename": name,
        "size": len(data),
        "content_type": content_type,
        "note": "把该 url 作为新闻的 cover_url 推送，平台会在入库时复制归档到 /images/cover/{新闻id}.<ext>（源文件保留）。",
    }
