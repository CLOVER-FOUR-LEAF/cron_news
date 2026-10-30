import os
import tempfile
import threading

from app.config import BASE_DIR

ENV_FILE = BASE_DIR / ".env"
_ENV_WRITE_LOCK = threading.Lock()


def read_env_file() -> dict[str, str]:
    env_vars = {}
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()
    return env_vars


def write_env_file(env_vars: dict[str, str]):
    """写回 .env：同目录临时文件 + os.replace，避免进程中断/并发读取看到半文件。"""
    with _ENV_WRITE_LOCK:
        lines = []
        written_keys = set()

        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line_stripped = line.strip()
                    if line_stripped and not line_stripped.startswith("#") and "=" in line_stripped:
                        key = line_stripped.split("=", 1)[0].strip()
                        if key in env_vars:
                            lines.append(f"{key}={env_vars[key]}\n")
                            written_keys.add(key)
                        else:
                            lines.append(line)
                    else:
                        lines.append(line)

        for key, value in env_vars.items():
            if key not in written_keys:
                lines.append(f"{key}={value}\n")

        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(ENV_FILE.parent),
                prefix=f".{ENV_FILE.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                tmp.writelines(lines)
                tmp.flush()
                os.fsync(tmp.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, ENV_FILE)
        except Exception:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise
