"""全局运行日志：按天滚动写入 logs/app.log，自动归档。

- 每天一个日志文件（app.log 为当天文件，前一天的自动改名归档如 app.log.2026-08-08），
  默认保留 30 天，之后自动清理。
- 同时输出到 stdout（Docker 部署可用 `docker logs` 查看）。
"""

import logging
import logging.handlers
from pathlib import Path

from app.config import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "app.log"
LOG_BACKUP_DAYS = 30

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

    # 每天零点轮转一次，归档文件名形如 app.log.2026-08-08，保留 30 天
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d"
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
