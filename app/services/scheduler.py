import asyncio
from datetime import datetime, timedelta

from app.config import settings
from app.database import async_session
from app.env_store import read_env_file
from app.services.ai_service import run_search_task
from app.services.recommend_service import run_recommend_task


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run: datetime | None = None
        self._next_run: float | None = None
        self._last_result: dict | None = None

    @property
    def status(self) -> dict:
        next_run_dt = None
        if self._next_run:
            try:
                next_run_dt = datetime.fromtimestamp(self._next_run).isoformat()
            except (ValueError, TypeError):
                pass

        return {
            "running": self._running,
            "cron": settings.SEARCH_CRON,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "next_run": next_run_dt,
            "last_result": self._last_result,
        }

    def _read_task_settings(self) -> tuple[str, int, int, str]:
        env = read_env_file()
        mode = env.get("TASK_MODE", "preset")
        interval = _parse_int(env.get("TASK_INTERVAL_HOURS", ""), 6)
        start = _parse_int(env.get("TASK_START_HOUR", ""), 0)
        cron = env.get("SEARCH_CRON", "")
        return mode, max(1, interval), start % 24, cron

    def _next_run_datetime(self) -> datetime:
        mode, interval, start, cron = self._read_task_settings()
        now = datetime.now()

        if mode == "preset":
            return self._next_from_preset(now, interval, start)
        return self._next_from_cron(cron, now)

    def _next_from_preset(self, now: datetime, interval: int, start: int) -> datetime:
        best: datetime | None = None
        for day_offset in range(0, 3):
            base = (now + timedelta(days=day_offset)).replace(
                hour=start, minute=0, second=0, microsecond=0
            )
            for k in range(0, 49):
                t = base + timedelta(hours=k * interval)
                if t > now and (best is None or t < best):
                    best = t
        return best or (now + timedelta(hours=interval))

    def _next_from_cron(self, cron: str, now: datetime) -> datetime:
        parts = cron.split()
        if len(parts) >= 2:
            m_part, h_part = parts[0], parts[1]
            minute = _parse_int(m_part, 0) % 60

            if h_part.startswith("*/"):
                step = max(1, _parse_int(h_part[2:], 6))
                return self._next_step(now, 0, minute, step)

            if "/" in h_part:
                head, _, tail = h_part.partition("/")
                start = _parse_int(head, 0) % 24
                step = max(1, _parse_int(tail, 6))
                return self._next_step(now, start, minute, step)

            if h_part.isdigit():
                t = now.replace(hour=int(h_part) % 24, minute=minute, second=0, microsecond=0)
                if t <= now:
                    t += timedelta(days=1)
                return t

        return now + timedelta(hours=6)

    def _next_step(self, now: datetime, start: int, minute: int, step: int) -> datetime:
        for day_offset in range(0, 3):
            base = (now + timedelta(days=day_offset)).replace(
                hour=start, minute=minute, second=0, microsecond=0
            )
            for k in range(0, 49):
                t = base + timedelta(hours=k * step)
                if t > now:
                    return t
        return now + timedelta(hours=step)

    def _recommend_enabled(self) -> bool:
        return read_env_file().get("AGENT_RECOMMEND_ENABLED", "") == "true"

    async def _run_search(self):
        try:
            self._running = True
            async with async_session() as session:
                result = await run_search_task(session)
                if self._recommend_enabled():
                    await run_recommend_task(session)
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                print(f"[Scheduler] 搜索完成: 新增 {result['total_new']} 条新闻")
        except Exception as e:
            self._last_result = {"error": str(e)}
            print(f"[Scheduler] 搜索失败: {e}")
        finally:
            self._running = False

    def _work_mode(self) -> str:
        return read_env_file().get("WORK_MODE", "")

    async def _loop(self):
        while True:
            try:
                if self._work_mode() != "autonomous":
                    self._next_run = None
                    await asyncio.sleep(15)
                    continue

                target = self._next_run_datetime()
                self._next_run = target.timestamp()

                while datetime.now() < target:
                    if self._work_mode() != "autonomous":
                        break
                    remaining = (target - datetime.now()).total_seconds()
                    await asyncio.sleep(min(30, max(1, remaining)))
                    refreshed = self._next_run_datetime()
                    if refreshed != target:
                        target = refreshed
                        self._next_run = target.timestamp()

                if self._work_mode() != "autonomous":
                    self._next_run = None
                    continue

                if settings.SEARCH_BASE_URL and settings.SEARCH_API_KEY:
                    await self._run_search()
                else:
                    print("[Scheduler] 搜索服务未配置，跳过本次执行")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Scheduler] 循环错误: {e}")
                await asyncio.sleep(60)

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        print(f"[Scheduler] 已启动，cron: {settings.SEARCH_CRON}")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            print("[Scheduler] 已停止")

    async def run_with_emit(self, emit):
        try:
            self._running = True
            async with async_session() as session:
                result = await run_search_task(session, emit=emit)
                if self._recommend_enabled():
                    await run_recommend_task(session, emit=emit)
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                return result
        except Exception as e:
            self._last_result = {"error": str(e)}
            await emit("error", f"任务异常: {e}")
            return None
        finally:
            self._running = False

    async def trigger_now(self):
        await self._run_search()
        return self._last_result


scheduler = Scheduler()
