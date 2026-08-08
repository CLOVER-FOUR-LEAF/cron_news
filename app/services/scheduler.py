import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import get_session
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import AgentRun
from app.services.ai_service import run_search_task
from app.services.recommend_service import run_recommend_task
from app.services.brief_service import run_brief_task

logger = get_logger("scheduler")


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
        self._last_skip: dict | None = None
        self._caught_up = False

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
            "last_skip": self._last_skip,
        }

    def _read_task_settings(self) -> tuple[str, int, int, str]:
        env = read_env_file()
        mode = env.get("TASK_MODE", "preset")
        interval = _parse_int(env.get("TASK_INTERVAL_HOURS", ""), 8)
        start = _parse_int(env.get("TASK_START_HOUR", ""), 0)
        cron = env.get("SEARCH_CRON", "")
        return mode, max(1, interval), start % 24, cron

    def _task_interval_hours(self) -> int:
        return self._read_task_settings()[1]

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

    def _make_collector(self, lines: list, agent: str = "定时资讯"):
        async def collect(event_type: str, text: str, **extra):
            lines.append({
                "t": event_type,
                "x": text,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "agent": agent,
            })
        return collect

    async def _save_run(self, lines: list, status: str, reason: str | None = None):
        """保存一条 AgentRun 运行日志（含「跳过」记录，保证任何调度决策都可见）。"""
        if not lines:
            lines = [{
                "t": "info" if status == "skipped" else "error",
                "x": reason or f"任务结束（{status}）",
                "ts": datetime.now().strftime("%H:%M:%S"),
                "agent": "定时资讯",
            }]
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
            logger.error("保存运行日志失败: %s", e)

    async def _record_skip(self, reason: str):
        self._last_skip = {"time": datetime.now().isoformat(), "reason": reason}
        logger.warning("定时任务跳过执行：%s", reason)
        await self._save_run([], "skipped", reason=reason)

    async def _run_search(self):
        lines: list = []
        status = "finished"
        try:
            self._running = True
            async with get_session() as session:
                result = await run_search_task(session, emit=self._make_collector(lines, "定时资讯"))
                if self._recommend_enabled():
                    await run_recommend_task(session, emit=self._make_collector(lines, "智能推荐"))
                if self._brief_enabled():
                    await run_brief_task(session, emit=self._make_collector(lines, "每日简报"))
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                logger.info("搜索任务完成：新增 %s 条新闻", result.get("total_new"))
        except Exception as e:
            status = "failed"
            lines.append({"t": "error", "x": f"任务异常: {e}", "ts": datetime.now().strftime("%H:%M:%S"), "agent": "定时资讯"})
            self._last_result = {"error": str(e)}
            logger.exception("搜索任务失败：%s", e)
        finally:
            self._running = False
            await self._save_run(lines, status)

    def _work_mode(self) -> str:
        return read_env_file().get("WORK_MODE", "")

    async def _search_ready(self) -> tuple[bool, str]:
        """检查搜索服务是否就绪，返回 (是否就绪, 未就绪原因)。"""
        try:
            from app.services.model_configs import get_active_endpoint

            async with get_session() as session:
                endpoint = await get_active_endpoint(session, "search")
                if not endpoint:
                    return False, "未启用任何搜索服务配置（请在「模型与服务配置」中新增并启用）"
                if not endpoint["api_key"]:
                    return False, f"搜索服务「{endpoint['provider_name']}」未填写 API Key"
                return True, ""
        except Exception as e:
            logger.error("检查搜索服务配置失败：%s", e)
            return False, f"检查搜索服务配置失败：{e}"

    def _stale_since_last_run(self, interval_hours: int) -> bool:
        env = read_env_file()
        raw = env.get("LAST_SEARCH_TIME", "")
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return True
        return (datetime.now() - last) > timedelta(hours=interval_hours)

    async def _loop(self):
        last_mode: str | None = None
        last_skip_reason: str | None = None
        while True:
            try:
                mode = self._work_mode()
                if mode != "autonomous":
                    # 非自主模式下定时任务不自动执行，但记录一次可见原因（辅助模式由外部 Agent 推送）
                    self._next_run = None
                    self._caught_up = False
                    skip_reason = (
                        "当前为辅助模式，定时任务不会自动执行（辅助模式下由外部 Agent 推送）；"
                        "请在「工作模式」中切换为自主模式"
                    )
                    if mode != last_mode:
                        logger.info("当前工作模式：%s，%s", mode or "未设置", skip_reason)
                        last_mode = mode
                        if skip_reason != last_skip_reason:
                            await self._record_skip(skip_reason)
                            last_skip_reason = skip_reason
                    await asyncio.sleep(15)
                    continue
                if last_mode != mode:
                    logger.info("工作模式：自主，定时任务已启用")
                    last_mode = mode

                ready, reason = await self._search_ready()
                if not ready:
                    # 未就绪时：记录一次原因，并每 2 分钟重检，避免一直等到下一个执行槽位
                    self._next_run = None
                    if reason != last_skip_reason:
                        await self._record_skip(reason)
                        last_skip_reason = reason
                    await asyncio.sleep(120)
                    continue

                last_skip_reason = None
                interval = self._task_interval_hours()
                target = self._next_run_datetime()
                catch_up = False

                # 启动补跑：若距上次采集超过一个周期，立即补跑一次，避免刚部署/重启后干等半天
                if not self._caught_up:
                    self._caught_up = True
                    if self._stale_since_last_run(interval):
                        logger.info("距上次采集已超过 %s 小时，启动后 30 秒内补跑一次", interval)
                        target = datetime.now() + timedelta(seconds=30)
                        catch_up = True

                self._next_run = target.timestamp()

                while datetime.now() < target:
                    if self._work_mode() != "autonomous":
                        break
                    remaining = (target - datetime.now()).total_seconds()
                    await asyncio.sleep(min(30, max(1, remaining)))
                    if not catch_up:
                        # 仅对「定时槽位」目标做配置变更兜底，补跑目标不可被覆盖
                        refreshed = self._next_run_datetime()
                        if refreshed != target:
                            target = refreshed
                            self._next_run = target.timestamp()

                if self._work_mode() != "autonomous":
                    self._next_run = None
                    continue

                ready, _ = await self._search_ready()
                if ready:
                    await self._run_search()
                else:
                    logger.warning("搜索服务未配置，跳过本次执行")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("调度循环错误：%s", e)
                await asyncio.sleep(60)

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("定时任务调度器已启动，cron: %s", settings.SEARCH_CRON)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("定时任务调度器已停止")

    async def run_with_emit(self, emit):
        lines: list = []

        def make_both(agent: str):
            collector = self._make_collector(lines, agent)

            async def both(event_type: str, text: str, **extra):
                await collector(event_type, text, **extra)
                await emit(event_type, text, **extra)

            return both

        status = "finished"
        try:
            self._running = True
            async with get_session() as session:
                result = await run_search_task(session, emit=make_both("定时资讯"))
                if self._recommend_enabled():
                    await run_recommend_task(session, emit=make_both("智能推荐"))
                if self._brief_enabled():
                    await run_brief_task(session, emit=make_both("每日简报"))
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
