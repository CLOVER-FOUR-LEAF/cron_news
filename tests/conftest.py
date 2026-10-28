"""测试环境：在导入应用前把数据库指向临时文件，避免污染开发数据。"""

import os
import tempfile

_fd, _db_path = tempfile.mkstemp(prefix="cron_news_test_", suffix=".db")
os.close(_fd)

os.environ["DB_PATH"] = _db_path
os.environ["AGENT_TOOL_PROTOCOL"] = "native"
os.environ["AGENT_CHAT_ENABLED"] = "true"


def pytest_sessionfinish(session, exitstatus):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_db_path + suffix)
        except OSError:
            pass
