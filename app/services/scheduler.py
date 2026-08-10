import asyncio
import json
import time
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
            "schedule": self._schedule_desc(),
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "next_run": next_run_dt,
            "last_result": self._last_result,
            "last_skip": self._last_skip,
        }

    def _schedule_desc(self) -> str:
        interval, start = self._read_task_settings()
        return f"每 {interval} 小时 · 每日 {start} 点起"

    def _read_task_settings(self) -> tuple[int, int]:
        env = read_env_file()
        interval = _parse_int(env.get("TASK_INTERVAL_HOURS", ""), 8)
        start = _parse_int(env.get("TASK_START_HOUR", ""), 0)
        return max(1, interval), start % 24

    def _task_interval_hours(self) -> int:
        return self._read_task_settings()[0]

    def _next_run_datetime(self) -> datetime:
        """固定触发时间点：每日从 start 点开始，每隔 interval 小时触发一次（24 小时内取最近的下一次）。"""
        interval, start = self._read_task_settings()
        now = datetime.now()
        return self._next_from_preset(now, interval, start)

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

    async def _run_search(self, source: str = "定时"):
        lines: list = []
        status = "finished"
        started = datetime.now()
        logger.info("任务开始执行（触发来源=%s）", source)
        try:
            self._running = True
            async with get_session() as session:
                logger.info("—— 阶段：新闻采集（搜索 + 撰稿）开始")
                result = await run_search_task(session, emit=self._make_collector(lines, "定时资讯"))
                logger.info("—— 阶段：新闻采集完成，新增 %s 条新闻", result.get("total_new"))
                if self._recommend_enabled():
                    logger.info("—— 阶段：智能推荐开始")
                    await run_recommend_task(session, emit=self._make_collector(lines, "智能推荐"))
                    logger.info("—— 阶段：智能推荐完成")
                if self._brief_enabled():
                    logger.info("—— 阶段：每日简报开始")
                    await run_brief_task(session, emit=self._make_collector(lines, "每日简报"))
                    logger.info("—— 阶段：每日简报完成")
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                logger.info(
                    "任务执行完成（触发来源=%s），耗时 %s 秒，新增 %s 条新闻",
                    source,
                    (datetime.now() - started).total_seconds(),
                    result.get("total_new"),
                )
        except Exception as e:
            status = "failed"
            lines.append({"t": "error", "x": f"任务异常: {e}", "ts": datetime.now().strftime("%H:%M:%S"), "agent": "定时资讯"})
            self._last_result = {"error": str(e)}
            logger.exception("任务执行失败（触发来源=%s）：%s", source, e)
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

    def _next_run_human(self) -> str:
        if not self._next_run:
            return "无（未就绪/非自主模式）"
        try:
            return datetime.fromtimestamp(self._next_run).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return "未知"

    async def _loop(self):
        last_mode: str | None = None
        last_skip_reason: str | None = None
        last_heartbeat = 0.0
        while True:
            try:
                mode = self._work_mode()

                # 心跳日志：每 5 分钟输出一次当前状态，便于排查「为什么没执行」
                if time.time() - last_heartbeat >= 300:
                    last_heartbeat = time.time()
                    ready, _ = await self._search_ready()
                    logger.info(
                        "调度器心跳：工作模式=%s，搜索就绪=%s，下次执行=%s，运行中=%s",
                        mode or "未设置",
                        "是" if ready else "否",
                        self._next_run_human(),
                        self._running,
                    )

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
                interval, start = self._read_task_settings()
                target = self._next_from_preset(datetime.now(), interval, start)
                catch_up = False

                # 启动补跑：若距上次采集超过一个周期，立即补跑一次，避免刚部署/重启后干等半天
                if not self._caught_up:
                    self._caught_up = True
                    if self._stale_since_last_run(interval):
                        logger.info("距上次采集已超过 %s 小时，启动后 30 秒内补跑一次", interval)
                        target = datetime.now() + timedelta(seconds=30)
                        catch_up = True

                self._next_run = target.timestamp()
                logger.info("定时任务下次执行：%s（目标时间）", target.strftime("%Y-%m-%d %H:%M:%S"))

                while datetime.now() < target:
                    if self._work_mode() != "autonomous":
                        break
                    remaining = (target - datetime.now()).total_seconds()
                    await asyncio.sleep(min(30, max(1, remaining)))
                    if not catch_up:
                        # 仅当「时间间隔/开始时间」真实变化时才重算目标，避免到点瞬间被
                        # _next_run_datetime()（严格大于当前时刻的下一个槽位）误覆盖而跳过一次执行
                        cur_interval, cur_start = self._read_task_settings()
                        if (cur_interval, cur_start) != (interval, start):
                            interval, start = cur_interval, cur_start
                            target = self._next_from_preset(datetime.now(), interval, start)
                            self._next_run = target.timestamp()
                            logger.info("任务配置变化，下次执行调整为：%s", target.strftime("%Y-%m-%d %H:%M:%S"))

                if self._work_mode() != "autonomous":
                    self._next_run = None
                    continue

                ready, _ = await self._search_ready()
                if ready:
                    await self._run_search("定时")
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
        logger.info("定时任务调度器已启动，%s", self._schedule_desc())

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("定时任务调度器已停止")

    async def run_with_emit(self, emit):
        lines: list = []
        started = datetime.now()
        logger.info("任务开始执行（触发来源=手动/页面流式）")

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
                logger.info("—— 阶段：新闻采集（搜索 + 撰稿）开始")
                result = await run_search_task(session, emit=make_both("定时资讯"))
                logger.info("—— 阶段：新闻采集完成，新增 %s 条新闻", result.get("total_new"))
                if self._recommend_enabled():
                    logger.info("—— 阶段：智能推荐开始")
                    await run_recommend_task(session, emit=make_both("智能推荐"))
                    logger.info("—— 阶段：智能推荐完成")
                if self._brief_enabled():
                    logger.info("—— 阶段：每日简报开始")
                    await run_brief_task(session, emit=make_both("每日简报"))
                    logger.info("—— 阶段：每日简报完成")
                await session.commit()
                self._last_result = result
                self._last_run = datetime.now()
                logger.info("任务执行完成，耗时 %s 秒", (datetime.now() - started).total_seconds())
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
        logger.info("手动触发「立即执行」按钮")
        await self._run_search("手动")
        return self._last_result


scheduler = Scheduler()
