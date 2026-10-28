"""Agent 协同：委派工具与任务锁。"""

import asyncio

import pytest

from app.services import agent_jobs
from app.services.agent import tools


def test_delegate_tools_registered():
    for name in ("delegate_collect", "delegate_brief", "delegate_recommend"):
        spec = tools.TOOLS[name]
        assert spec.kind == "delegate"
        assert spec.timeout > 60  # 委派任务需要长超时


def test_is_delegate():
    assert tools.is_delegate("delegate_collect") is True
    assert tools.is_delegate("search_news") is False
    assert tools.is_delegate("not_a_tool") is False


def test_delegate_tools_are_in_schemas():
    names = {s["function"]["name"] for s in tools.tool_schemas()}
    assert {"delegate_collect", "delegate_brief", "delegate_recommend"} <= names


@pytest.mark.asyncio
async def test_job_slot_rejects_concurrent_job():
    """同一时间只允许一个 Agent 任务，第二个立即返回 busy 而不是排队。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        async with agent_jobs.job_slot("测试"):
            started.set()
            await release.wait()
            return {"ok": True}

    task = asyncio.create_task(slow())
    await started.wait()

    # 占用期间再申请应该抛 JobBusy
    with pytest.raises(agent_jobs.JobBusy):
        async with agent_jobs.job_slot("第二个"):
            pass

    release.set()
    await task
    # 释放后可以再次获取
    async with agent_jobs.job_slot("第三个"):
        pass


@pytest.mark.asyncio
async def test_run_job_returns_busy_result():
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with agent_jobs.job_slot("占位"):
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await started.wait()

    result = await agent_jobs._run_job("被拒绝", lambda: asyncio.sleep(0))
    assert result["ok"] is False
    assert result["busy"] is True

    release.set()
    await holder
