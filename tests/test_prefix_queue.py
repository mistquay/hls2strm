import asyncio
import time

from hls2strm.prefix_crawl import cancel, create
from hls2strm.prefix_store import PREFIX_HEADS_SQL, progress_page, sequence_progress

from .test_engine import wait_job
from .test_prefix_crawl import body, listing, setup


def test_engine_finishes_pages_and_parallel_details_before_next_prefix(make_store, boot, monkeypatch):
    async def run():
        db, engine, fetcher, scope = await setup(make_store, boot)
        fetcher.pages["missav"] = {
            "/cn/search/ABC?page=1": listing("abc-001", last=2),
            "/cn/search/ABC?page=2": listing("abc-002", last=2),
            "/cn/search/DEF?page=1": listing("def-001"),
        }
        entered, release = asyncio.Event(), asyncio.Event()
        details = []

        async def detail(site, key, **kwargs):
            details.append(key)
            if key.startswith("abc-"):
                if len(details) == 2:
                    entered.set()
                await release.wait()

        monkeypatch.setattr(engine, "fetch_detail", detail)
        jid = await create(engine, body(scope, prefixes=["DEF", "ABC"], detail=True))
        await engine.start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert len(engine.running) == 2  # 当前前缀的详情仍可并发。
            assert all("DEF" not in call for call in fetcher.calls)
            release.set()
            assert (await wait_job(db, jid))["status"] == "done"
        finally:
            release.set()
            await engine.stop()
        assert fetcher.calls == [f"missav:{path}" for path in fetcher.pages["missav"]]
        assert details == ["abc-001", "abc-002", "def-001"]
    asyncio.run(run())


def test_cooldown_keeps_prefix_order_without_blocking_other_jobs(make_store, boot):
    async def run():
        db, engine, _, scope = await setup(make_store, boot)
        jid = await create(engine, body(scope, prefixes=["ABC", "DEF"]))
        first = await db.claim_task()
        assert first["target"] == "missav/ABC/1"
        await db.finish_task(first["id"], "pending", next_run_at=int(time.time()) + 3600)
        assert await db.claim_task() is None  # ABC 等待重试时不能改抓 DEF。
        other = await db.create_job("videos", "独立任务", {})
        await db.add_tasks(other, "detail", ["other"], site="missav")
        independent = await db.claim_task()
        assert independent["job_id"] == other
        await db.finish_task(independent["id"], "done")
        await db.finish_task(first["id"], "failed")
        second = await db.claim_task()
        assert second["target"] == "missav/DEF/1"
        await db.finish_task(second["id"], "done")
        await db.retry_failed_tasks(jid)
        assert (await db.claim_task())["target"] == "missav/ABC/1"
    asyncio.run(run())


def test_prefix_cancel_drains_running_work_before_advancing(make_store, boot):
    async def run():
        db, engine, _, scope = await setup(make_store, boot)
        jid = await create(engine, body(scope, prefixes=["ABC", "DEF"]))
        first = await db.claim_task()
        await db.add_tasks(jid, "prefix_detail", ["missav/ABC/abc-001"], site="missav")
        await cancel(engine, jid, "missav", "ABC")
        assert await db.claim_task() is None
        await db.finish_task(first["id"], "cancelled")
        assert (await db.claim_task())["target"] == "missav/DEF/1"
        await db.retry_failed_tasks(jid)
        assert not any(t["status"] == "pending" and "/ABC/" in t["target"] for t in await db.list_tasks(jid))
    asyncio.run(run())


def test_legacy_running_prefix_drains_and_restart_uses_saved_order(make_store, boot):
    async def run():
        db, engine, _, scope = await setup(make_store, boot)
        jid = await create(engine, body(scope, prefixes=["ABC", "DEF"], sites=["jable", "missav"]))
        # 模拟旧版本正在抓靠后的前缀；不再扩大发散执行。
        await db._write("UPDATE tasks SET status='running',attempts=1 WHERE job_id=? AND target='missav/DEF/1'", (jid,))
        assert await db.claim_task() is None
        await db.reset_running_tasks()
        assert (await progress_page(db, jid, q="DEF"))["items"][1]["scan_status"] == "waiting"
        assert (await progress_page(db, jid, q="DEF", state="queued"))["total"] == 1
        assert (await db.claim_task())["target"] == "jable/ABC/1"
        await engine.set_job_status(jid, "pause")
        assert await db.claim_task() is None
        await db.reset_running_tasks()
        await engine.set_job_status(jid, "resume")
        assert (await db.claim_task())["target"] == "jable/ABC/1"
    asyncio.run(run())


def test_sequence_counts_and_filters_include_detail_work(make_store, boot):
    async def run():
        db, engine, _, scope = await setup(make_store, boot)
        jid = await create(engine, body(scope, prefixes=["ABC", "DEF", "GHI"]))
        rows = (await progress_page(db, jid))["items"]
        assert all(row["scan_status"] == "queued" for row in rows)
        first = await db.claim_task()
        await db.finish_task(first["id"], "done")
        await db._write("UPDATE prefix_runs SET listing_complete=1,last_page=1 WHERE job_id=? AND prefix='ABC'", (jid,))
        await db.add_tasks(jid, "prefix_detail", ["missav/ABC/abc-001"], site="missav")
        db.job_cache.clear()
        state = await sequence_progress(db, jid)
        assert state == {"total": 3, "finished": 0, "waiting": 2, "failed": 0, "cancelled": 0,
                         "current": {"site": "missav", "prefix": "ABC", "position": 1}}
        assert [r["prefix"] for r in (await progress_page(db, jid, state="queued"))["items"]] == ["DEF", "GHI"]
        detail = await db.claim_task()
        assert detail["kind"] == "prefix_detail"
        await db.finish_task(detail["id"], "failed")
        await cancel(engine, jid, "missav", "DEF")
        state = await sequence_progress(db, jid)
        assert state["finished"] == 2 and state["failed"] == 1 and state["cancelled"] == 1
        assert state["current"]["position"] == 3
        assert [r["prefix"] for r in (await progress_page(db, jid, state="unfinished"))["items"]] == ["GHI"]
        assert [r["prefix"] for r in (await progress_page(db, jid, state="failed"))["items"]] == ["ABC"]
        assert (await progress_page(db, jid, q="gh", state="queued"))["total"] == 1
    asyncio.run(run())


def test_prefix_head_uses_full_expression_index(make_store, boot):
    async def run():
        db, engine, _, scope = await setup(make_store, boot)
        jid = await create(engine, body(scope, prefixes=["ABC", "DEF"]))
        plan = await db._all("EXPLAIN QUERY PLAN " + PREFIX_HEADS_SQL + " SELECT * FROM prefix_heads")
        assert any("tasks_prefix_progress (job_id=? AND site=? AND <expr>=?" in row["detail"] for row in plan)
        assert (await db._one(PREFIX_HEADS_SQL + " SELECT head FROM prefix_heads WHERE job_id=?", (jid,)))["head"] == "missav/ABC"
    asyncio.run(run())
