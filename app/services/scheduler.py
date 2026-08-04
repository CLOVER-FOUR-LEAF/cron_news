import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import get_session
from app.env_store import read_env_file
from app.models import AgentRun
from app.services.ai_service import run_search_task
from app.services.recommend_service import run_recommend_task
from app.services.brief_service import run_brief_task


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
        interval = _parse_int(env.get("TASK_INTERVAL_HOURS", ""), 8)
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

    def _brief_enabled(self) -> bool:
        return read_env_file().get("AGENT_BRIEF_ENABLED", "") == "true"

    def _make_collector(self, lines: list):
        async def collect(event_type: str, text: str, **extra):
            lines.append({"t": event_type, "x": text, "ts": datetime.now().strftime("%H:%M:%S")})
        return collect

    async def _save_run(self, lines: list, status: str):
        if not lines:
            return
        try:
            async with get_session() as session:
                session.add(AgentRun(
                    started_at=datetime.now(),
                    status=status,
                    content=json.dumps(lines, ensure_ascii=False),
                ))
                await session.flush()
                result = await session.execute(select(AgentRun).order_by(AgentRun.id.desc()).offset(10))
                for old in result.scalars().all():
                    await session.delete(old)
                await session.commit()
        except Exception as e:
            print(f"[Scheduler] 保存运行日志失败: {e}")

    async def _run_search(self):
        lines: list = []
        status = "finished"
        try:
            self._running = True
            async with get_session() as session:
                result = await run_search_task(session, emit=self._make_collector(lines))
                if self._recommend_enabled():
                    await run_recommend_task(session, emit=self._make_collector(lines))
                if self._brief_enabled():
                    await run_brief_task(session, emit=self._make_collector(lines))
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                print(f"[Scheduler] 搜索完成: 新增 {result['total_new']} 条新闻")
        except Exception as e:
            status = "failed"
            lines.append({"t": "error", "x": f"任务异常: {e}", "ts": datetime.now().strftime("%H:%M:%S")})
            self._last_result = {"error": str(e)}
            print(f"[Scheduler] 搜索失败: {e}")
        finally:
            self._running = False
            await self._save_run(lines, status)

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
        lines: list = []
        collector = self._make_collector(lines)

        async def both(event_type: str, text: str, **extra):
            await collector(event_type, text, **extra)
            await emit(event_type, text, **extra)

        status = "finished"
        try:
            self._running = True
            async with get_session() as session:
                result = await run_search_task(session, emit=both)
                if self._recommend_enabled():
                    await run_recommend_task(session, emit=both)
                if self._brief_enabled():
                    await run_brief_task(session, emit=both)
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                return result
        except Exception as e:
            status = "failed"
            self._last_result = {"error": str(e)}
            await emit("error", f"任务异常: {e}")
            return None
        finally:
            self._running = False
            await self._save_run(lines, status)

    async def trigger_now(self):
        await self._run_search()
        return self._last_result


scheduler = Scheduler()
