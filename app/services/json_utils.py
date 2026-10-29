"""从模型输出中稳健地提取第一个 JSON 对象。

模型常会在 JSON 前后加说明、代码围栏或多余花括号；这里逐字符尝试 raw_decode，
避免因为第一个 `{` 不是对象起点而整段解析失败。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            obj, _ = decoder.raw_decode(raw[match.start() :])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None
