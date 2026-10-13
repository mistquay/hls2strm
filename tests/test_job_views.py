import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from hls2strm import job_views
from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.engine import PRIORITY_LIST

from .conftest import FakeFetcher, fixture
from .test_engine import build, wait_job


async def completed_list(db):
    job = await db.create_job("crawl", "MissAV 列表", {"site": "missav"}, status="paused")
    await db.add_tasks(job, "list", [1, 2], PRIORITY_LIST, "missav")
    await db._write("UPDATE tasks SET status='done' WHERE job_id=?", (job,))
    await db.add_tasks(job, "probe", ["123av:1", "123av:2"], site="123av")
    await db.update_job(job, status="running", state={"list_complete": True})
    return job


@pytest.mark.parametrize("state,phase,message", [
    ("cooling", "background_waiting", "123AV 补源 2 项，站点冷却"),
    ("retry", "background_waiting", "123AV 补源 2 项，约"),
    ("disabled", "background_waiting", "123AV 补源 2 项，站点已停用"),
    ("running", "background_running", "123AV 补源 2 项，1 项执行中"),
    ("paused", "paused", "任务已暂停"),
    ("engine_paused", "scheduler_paused", "任务已暂停"),
])
def test_completed_list_explains_actual_remaining_site(make_store, boot, state, phase, message):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        jid = await completed_list(db)
        if state == "cooling":
            engine.blocked["123av"] = time.time() + 300
        elif state == "retry":
            await db._write("UPDATE tasks SET next_run_at=? WHERE job_id=? AND kind='probe'", (int(time.time()) + 300, jid))
        elif state == "disabled":
            await store.update({"sites": {"123av": {"enabled": False}}})
        elif state == "running":
            await db.claim_task()
        elif state == "paused":
            await db.update_job(jid, status="paused")
        else:
            engine.pause()
        result = await job_views.details(engine, await db.get_job(jid))
        assert result["phase"] == phase
        assert result["phase_detail"].startswith("MissAV 列表抓取已完成。")
        assert message in result["phase_detail"]
        assert "list_complete" in result["state"]
        assert (await db.get_job(jid))["status"] == ("paused" if state == "paused" else "running")
    asyncio.run(run())


def test_pending_list_is_never_presented_as_complete(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        jid = await completed_list(db)
        # 即使旧状态残留 list_complete，也以实际未结束列表子任务为准。
        await db.add_tasks(jid, "list", [3], PRIORITY_LIST, "missav")
        engine.blocked["123av"] = time.time() + 300
        result = await job_views.details(engine, await db.get_job(jid))
        assert result["phase"] == "queued"
        assert "列表抓取已完成" not in result["phase_detail"]
        assert "MissAV 列表抓取 1 项" in result["phase_detail"]
        assert "123AV 补源 2 项，站点冷却" in result["phase_detail"]
    asyncio.run(run())


def test_job_list_and_detail_agree_on_background_wait(tmp_path):
    with TestClient(create_app(BootConfig(data_dir=tmp_path))) as client:
        ctx = client.app.state.ctx
        async def seed():
            await ctx.store.update({"health_interval": 0})
            ctx.engine.blocked["123av"] = time.time() + 3600
            return await completed_list(ctx.db)
        jid = client.portal.call(seed)
        listing = client.get("/api/jobs?paged=true&status=active").json()
        job = next(j for j in listing["items"] if j["id"] == jid)
        detail = client.get(f"/api/jobs/{jid}").json()
        assert job["phase"] == detail["phase"] == "background_waiting"
        for result in (job, detail):
            assert "MissAV 列表抓取已完成" in result["phase_detail"]
            assert "123AV 补源 2 项，站点冷却" in result["phase_detail"]
            assert result["tasks"] == {"done": 2, "pending": 2}


def test_restart_resumes_missav_list_while_other_site_waits(make_store, boot):
    async def run():
        db, store = await make_store()
        await store.update({"download_cover": False, "quality_capture": False, "health_interval": 0})
        fetcher = FakeFetcher("", {})
        fetcher.pages["missav"] = {"/cn/new?page=1": fixture("missav_list.html")}
        engine = build(db, store, boot, fetcher)
        await engine.reload_libraries()
        jid = await engine.create_crawl("/cn/new", site="missav", end_page=1, detail=False)
        interrupted = await db.claim_task()
        assert interrupted["site"] == "missav"
        other = await db.create_job("probe", "123AV 补源", {})
        await db.add_tasks(other, "probe", ["123av:1"], site="123av")
        await db._write("UPDATE tasks SET next_run_at=? WHERE job_id=?", (int(time.time()) + 3600, other))
        # 冷却内存随重启消失，但持久化的 next_run_at 仍要只影响这个站。
        engine = build(db, store, boot, fetcher)
        await engine.start()
        try:
            assert (await wait_job(db, jid))["state"]["list_complete"]
            assert await db.task_counts(jid) == {"done": 1}
            assert await db.task_counts(other) == {"pending": 1}
            assert "missav:/cn/new?page=1" in fetcher.calls
            assert not any(call.startswith("123av:") for call in fetcher.calls)
        finally:
            await engine.stop()
    asyncio.run(run())
