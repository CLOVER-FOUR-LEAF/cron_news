"""全局运行日志：写入 logs/app.log（按大小轮转），同时输出到 stdout。

Docker 部署时可用 `docker logs` 查看，也可挂载 logs 目录持久化查看。
"""

import logging
import logging.handlers
from pathlib import Path

from app.config import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "app.log"

_configured = False


def setup_logging():
    global _configured
    if _configured:
        return
    _configured = True
    try:
        LOGS_DIR.mkdir(exist_ok=True)
    except OSError:
        pass

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def get_logger(name: str = "app") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
