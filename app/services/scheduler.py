import asyncio
import json
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import get_session
from app.env_store import read_env_file
from app.logging_setup import get_logger
from app.models import AgentRun
from app.services.ai_service import run_search_task
from app.services.recommend_service import run_recommend_task
from app.services.brief_service import run_brief_task
from app.services import smart_schedule

logger = get_logger("scheduler")


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _parse_datetime(raw: str) -> datetime | None:
    """解析 LAST_SEARCH_TIME；统一转成 naive 本地时间，避免 aware/naive 相减报错。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run: datetime | None = None
        self._last_attempt: datetime | None = None
        self._next_run: float | None = None
        self._last_result: dict | None = None
        self._last_skip: dict | None = None
        self._caught_up = False
        self._next_reason: str = ""
        self._smart_profile: smart_schedule.ActivityProfile | None = None
        self._smart_profile_at: float = 0.0
        self._smart_fingerprint: str = ""

    @property
    def status(self) -> dict:
        next_run_dt = None
        if self._next_run:
            try:
                next_run_dt = datetime.fromtimestamp(self._next_run).isoformat()
            except (ValueError, TypeError):
                pass

        last_run = self._last_run_dt()
        return {
            "running": self._running,
            "schedule": self._schedule_desc(),
            "schedule_mode": self._schedule_mode(),
            "next_reason": self._next_reason,
            "smart": self._smart_status(),
            "last_run": last_run.isoformat() if last_run else None,
            "last_search_time": read_env_file().get("LAST_SEARCH_TIME") or None,
            "next_run": next_run_dt,
            "last_result": self._last_result,
            "last_skip": self._last_skip,
        }

    def _smart_status(self) -> dict | None:
        if self._schedule_mode() != "smart":
            return None
        profile = self._smart_profile
        if profile is None:
            # 调度循环还没跑第一轮时，先按向导里的时间描述展示一个临时画像
            try:
                from app.services import preferences

                plan = preferences.schedule_plan()
                if plan.get("preferred_hours"):
                    profile = smart_schedule.build_profile(
                        [], datetime.now(), preferred_hours=plan["preferred_hours"]
                    )
            except Exception:
                profile = None
        if profile is None:
            return {
                "ready": False,
                "source": "none",
                "events": 0,
                "active_days": 0,
                "windows": [],
                "reason": "正在学习站内阅读与操作习惯",
                "lead_minutes": self._lead_minutes(),
                "next_reason": self._next_reason,
            }
        data = profile.to_dict()
        data["lead_minutes"] = self._lead_minutes()
        data["next_reason"] = self._next_reason
        return data

    def _schedule_desc(self) -> str:
        interval, start = self._read_task_settings()
        return smart_schedule.describe_mode(
            self._schedule_mode(), interval, start, self._lead_minutes()
        )

    def _schedule_mode(self) -> str:
        raw = (read_env_file().get("TASK_SCHEDULE_MODE", "") or "").strip().lower()
        return "smart" if raw == "smart" else "fixed"

    def _lead_minutes(self) -> int:
        return max(5, min(_parse_int(read_env_file().get("TASK_SMART_LEAD_MINUTES", ""), 45), 180))

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

    def _last_run_dt(self) -> datetime | None:
        """最近一次采集时间：取进程内记录与 LAST_SEARCH_TIME 中较新的一个。

        对话 Agent 委派的采集、或采集后简报失败时，只有 LAST_SEARCH_TIME 会更新；
        只看 self._last_run 会漏掉这些情况，导致智能模式重复采集。
        """
        candidates: list[datetime] = []
        if self._last_run:
            candidates.append(self._last_run)
        parsed = _parse_datetime(read_env_file().get("LAST_SEARCH_TIME", ""))
        if parsed:
            candidates.append(parsed)
        return max(candidates) if candidates else None

    def invalidate_smart_profile(self) -> None:
        """偏好描述变化后立即失效画像缓存，让下一次调度重算。"""
        self._smart_profile = None
        self._smart_profile_at = 0.0
        self._smart_fingerprint = ""

    async def _get_smart_profile(self) -> smart_schedule.ActivityProfile:
        """行为画像缓存 10 分钟，避免调度循环每 30 秒查库。"""
        from app.services import preferences

        now = datetime.now()
        plan = preferences.schedule_plan()
        preferred = plan.get("preferred_hours") or []
        fingerprint = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        if (
            self._smart_profile is not None
            and time.time() - self._smart_profile_at < 600
            and fingerprint == self._smart_fingerprint
        ):
            return self._smart_profile
        try:
            async with get_session() as session:
                events = await smart_schedule.collect_activity(session, now)
            profile = smart_schedule.build_profile(events, now, preferred_hours=preferred)
        except Exception as exc:
            logger.warning("学习用户活跃时段失败，回退固定频率：%s", exc)
            profile = smart_schedule.build_profile([], now, preferred_hours=preferred)
        self._smart_profile = profile
        self._smart_profile_at = time.time()
        self._smart_fingerprint = fingerprint
        logger.info(
            "智能调度画像已刷新：来源=%s，%s 次行为，%s 个活跃日，窗口=%s",
            profile.source,
            profile.events,
            profile.active_days,
            [item["label"] for item in profile.windows],
        )
        return profile

    async def _compute_target(self, now: datetime) -> datetime:
        interval, start = self._read_task_settings()
        if self._schedule_mode() != "smart":
            self._next_reason = "固定频率"
            return self._next_from_preset(now, interval, start)

        profile = await self._get_smart_profile()
        # 失败的任务也会更新 _last_attempt；把它纳入最小间隔，避免模型/网络异常时
        # 因为「从未成功过」而立刻重试、形成高频调用。
        candidates = [dt for dt in (self._last_run, self._last_attempt, self._last_run_dt()) if dt]
        last_activity = max(candidates) if candidates else None
        if last_activity and last_activity > now:
            # 时钟回拨或未来时间不应把后续采集一直推到未来
            last_activity = now
        target, reason = smart_schedule.next_smart_run(
            profile,
            now,
            interval,
            start,
            last_activity,
            self._lead_minutes(),
        )
        self._next_reason = reason
        return target

    async def _search_ready(self) -> tuple[bool, str]:
        """检查是否存在可用搜索能力：独立搜索服务或大模型内置联网。"""
        try:
            from app.services.model_configs import get_effective_search_endpoint

            async with get_session() as session:
                endpoint = await get_effective_search_endpoint(session)
                if not endpoint:
                    return False, (
                        "未配置可用的搜索服务，且当前大语言模型没有检测到内置联网搜索"
                        "（请在「模型与服务配置」中配置，或测试大模型的内置联网能力）"
                    )
                return True, ""
        except Exception as e:
            logger.error("检查搜索服务配置失败：%s", e)
            return False, f"检查搜索服务配置失败：{e}"

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

    async def _embed_new_news(self, lines: list):
        """采集完成后补齐向量，失败不影响主流程。"""
        try:
            from app.services import embeddings

            result = await embeddings.backfill()
            if result.get("embedded"):
                lines.append({
                    "t": "info",
                    "x": f"向量索引已更新 {result['embedded']} 条",
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "agent": "向量索引",
                })
                logger.info("向量索引已更新 %s 条", result["embedded"])
            elif result.get("reason") == "embedding_not_configured":
                logger.debug("未配置向量模型，跳过向量索引")
        except Exception as exc:
            logger.warning("采集后向量索引失败（不影响采集）：%s", exc)

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
        from app.services.agent_jobs import JobBusy, job_slot

        lines: list = []
        status = "finished"
        started = datetime.now()
        logger.info("任务开始执行（触发来源=%s）", source)
        try:
            self._running = True
            self._last_attempt = datetime.now()
            # 与对话助手的委派任务共用一把锁，避免同时采集/重复调用模型
            async with job_slot("定时采集"):
                async with get_session() as session:
                    logger.info("—— 阶段：新闻采集（搜索 + 撰稿）开始")
                    result = await run_search_task(session, emit=self._make_collector(lines, "定时资讯"))
                    await session.commit()
                    logger.info("—— 阶段：新闻采集完成，新增 %s 条新闻", result.get("total_new"))
                    await self._embed_new_news(lines)
                    if self._recommend_enabled():
                        logger.info("—— 阶段：智能推荐开始")
                        await run_recommend_task(session, emit=self._make_collector(lines, "智能推荐"))
                        await session.commit()
                        logger.info("—— 阶段：智能推荐完成")
                    if self._brief_enabled():
                        logger.info("—— 阶段：每日简报开始")
                        await run_brief_task(session, emit=self._make_collector(lines, "每日简报"))
                        await session.commit()
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
        except JobBusy as e:
            status = "skipped"
            reason = f"另一个 Agent 任务正在执行，跳过本次：{e}"
            self._last_skip = {"time": datetime.now().isoformat(), "reason": reason}
            logger.warning(reason)
            lines.append({
                "t": "info", "x": reason,
                "ts": datetime.now().strftime("%H:%M:%S"), "agent": "定时资讯",
            })
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

    def _stale_since_last_run(self, interval_hours: int) -> bool:
        env = read_env_file()
        raw = env.get("LAST_SEARCH_TIME", "")
        if not raw:
            return True
        last = _parse_datetime(raw)
        if last is None:
            return True
        if last > datetime.now():
            # 时钟回拨 / 手工填了未来时间：视为刚刚采集过，避免长时间不执行
            return False
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
                settings = (interval, start, self._schedule_mode(), self._lead_minutes())
                target = await self._compute_target(datetime.now())
                catch_up = False

                # 启动补跑：若距上次采集超过一个周期，立即补跑一次，避免刚部署/重启后干等半天
                if not self._caught_up:
                    self._caught_up = True
                    if self._stale_since_last_run(interval):
                        logger.info("距上次采集已超过 %s 小时，启动后 30 秒内补跑一次", interval)
                        target = datetime.now() + timedelta(seconds=30)
                        self._next_reason = "启动补跑（距上次采集过久）"
                        catch_up = True

                self._next_run = target.timestamp()
                logger.info(
                    "定时任务下次执行：%s（%s）",
                    target.strftime("%Y-%m-%d %H:%M:%S"),
                    self._next_reason,
                )
                last_smart_check = time.time()

                while datetime.now() < target:
                    if self._work_mode() != "autonomous":
                        break
                    remaining = (target - datetime.now()).total_seconds()
                    await asyncio.sleep(min(30, max(1, remaining)))
                    if not catch_up:
                        # 仅当「时间间隔/开始时间/调度方式/提前量」真实变化时才重算目标，
                        # 避免到点瞬间被严格大于当前时刻的下一个槽位误覆盖而跳过一次执行
                        cur_interval, cur_start = self._read_task_settings()
                        cur_settings = (
                            cur_interval,
                            cur_start,
                            self._schedule_mode(),
                            self._lead_minutes(),
                        )
                        if cur_settings != settings:
                            interval, start = cur_interval, cur_start
                            settings = cur_settings
                            target = await self._compute_target(datetime.now())
                            self._next_run = target.timestamp()
                            logger.info(
                                "任务配置变化，下次执行调整为：%s（%s）",
                                target.strftime("%Y-%m-%d %H:%M:%S"),
                                self._next_reason,
                            )
                            last_smart_check = time.time()
                        elif settings[2] == "smart" and time.time() - last_smart_check >= 300:
                            # 智能模式：用户行为画像可能变化，每 5 分钟重算一次目标
                            last_smart_check = time.time()
                            new_target = await self._compute_target(datetime.now())
                            if abs((new_target - target).total_seconds()) > 300:
                                logger.info(
                                    "智能调度目标变化：%s → %s（%s）",
                                    target.strftime("%Y-%m-%d %H:%M:%S"),
                                    new_target.strftime("%Y-%m-%d %H:%M:%S"),
                                    self._next_reason,
                                )
                                target = new_target
                                self._next_run = target.timestamp()

                if self._work_mode() != "autonomous":
                    self._next_run = None
                    continue

                ready, _ = await self._search_ready()
                if ready:
                    await self._run_search("定时")
                else:
                    logger.warning("搜索能力未就绪，跳过本次执行")
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
        from app.services.agent_jobs import JobBusy, job_slot

        try:
            self._running = True
            self._last_attempt = datetime.now()
            async with job_slot("手动采集"):
                async with get_session() as session:
                    logger.info("—— 阶段：新闻采集（搜索 + 撰稿）开始")
                    result = await run_search_task(session, emit=make_both("定时资讯"))
                    await session.commit()
                    logger.info("—— 阶段：新闻采集完成，新增 %s 条新闻", result.get("total_new"))
                    await self._embed_new_news(lines)
                    if self._recommend_enabled():
                        logger.info("—— 阶段：智能推荐开始")
                        await run_recommend_task(session, emit=make_both("智能推荐"))
                        await session.commit()
                        logger.info("—— 阶段：智能推荐完成")
                    if self._brief_enabled():
                        logger.info("—— 阶段：每日简报开始")
                        await run_brief_task(session, emit=make_both("每日简报"))
                        await session.commit()
                        logger.info("—— 阶段：每日简报完成")
                    await session.commit()
                    self._last_result = result
                    self._last_run = datetime.now()
                    logger.info("任务执行完成，耗时 %s 秒", (datetime.now() - started).total_seconds())
                    return result
        except JobBusy as e:
            status = "skipped"
            self._last_skip = {"time": datetime.now().isoformat(), "reason": str(e)}
            logger.warning("手动采集跳过：%s", e)
            await emit("info", f"另一个 Agent 任务正在执行，已跳过：{e}")
            return None
        except Exception as e:
            status = "failed"
            self._last_result = {"error": str(e)}
            await emit("error", f"任务异常: {e}")
            return None
        finally:
            self._running = False
            await self._save_run(lines, status)

    async def trigger_now(self):
        if self._work_mode() != "autonomous":
            raise RuntimeError("辅助模式下平台不执行内置采集；请由外部 Agent 推送新闻")
        logger.info("手动触发「立即执行」按钮")
        await self._run_search("手动")
        return self._last_result


scheduler = Scheduler()
