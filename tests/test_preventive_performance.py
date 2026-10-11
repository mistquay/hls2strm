"""大库后台工作与播放隔离、排序语义和任务准备断点回归。"""

import asyncio
import threading
import time

import pytest

from hls2strm.db import VideoQuery
from hls2strm.engine import TaskStopped
from hls2strm.quality import Quality
from hls2strm.sites import SourceDetail, SourceItem
from .conftest import FakeFetcher
from .test_engine import build


@pytest.mark.parametrize("operation", ["libraries", "jobs", "queue", "counts", "history"])
def test_management_query_cannot_queue_playback_read(make_store, operation):
    async def run():
        db, store = await make_store()
        await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", store.current.site_rank)
        job = await db.create_job("quality", "test", {})
        await db.add_tasks(job, "quality", ["one"])
        methods = {"libraries": db.list_libraries, "jobs": db.list_jobs, "queue": db.queue_stats,
                   "counts": lambda: db.task_counts(job), "history": lambda: db.history.counts([job])}
        entered, release = threading.Event(), threading.Event()

        def pause_management(sql):
            if sql.startswith("SELECT") and "FROM videos WHERE slug=" not in sql:
                entered.set()
                release.wait(3)

        # 同时拦截两个连接：只有走独立批量连接，快速读取才能继续。
        await db.reader.set_trace_callback(pause_management)
        await db.bulk_reader.set_trace_callback(pause_management)
        task = asyncio.create_task(methods[operation]())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # 播放 SELECT 不应被拦截；批量连接上的管理查询仍未完成。
            assert (await asyncio.wait_for(db.get_video("abc-001"), 1))["title"] == "ABC"
            assert not task.done()
        finally:
            release.set()
            await task
            await db.reader.set_trace_callback(None)
            await db.bulk_reader.set_trace_callback(None)
    asyncio.run(run())


def test_playback_writes_keep_facets_but_metadata_invalidates(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        detail = SourceDetail(key="abc-001", code="ABC-001", title="ABC", models=[{"id": "m1", "name": "Actor"}])
        vid = await db.upsert_detail("jable", detail, "abc-001", store.current.site_rank)
        src = (await db.get_sources(vid))[0]
        loads = []
        original = db._facet
        async def load(*args):
            loads.append(1)
            return await original(*args)
        monkeypatch.setattr(db, "_facet", load)
        assert (await db.facet("models"))[0]["n"] == 1
        await db.set_stream(src["id"], "https://test.invalid/media.m3u8", int(time.time())+86400)
        await db.source_failed(src["id"], "test failure")
        await db.set_lines(src["id"], [("EVS", "https://test.invalid/player")])
        line = (await db.get_lines(src["id"]))[0]
        await db.set_line_stream(line["id"], "https://test.invalid/line.m3u8", None, "vidhide")
        await db.use_line(src["id"], (await db.get_lines(src["id"]))[0])
        await db.set_quality(src["id"], line["id"], Quality([720], "master"))
        assert (await db.facet("models"))[0]["n"] == 1 and len(loads) == 1
        detail.models = [{"id": "m2", "name": "Other"}]
        await db.upsert_detail("jable", detail, "abc-001", store.current.site_rank)
        assert [r["item"] for r in await db.facet("models")] == ["m2"]
        await db.mark_source_gone("jable", "abc-001")
        assert await db.facet("models") == [] and len(loads) == 3
    asyncio.run(run())


@pytest.mark.parametrize("sort", ["duration", "code", "views", "updated"])
@pytest.mark.parametrize("desc", [False, True])
def test_sort_index_keeps_empty_values_last_and_stable_ties(make_store, sort, desc):
    async def run():
        db, _ = await make_store()
        column = {"duration": "duration", "code": "code_key", "views": "views", "updated": "updated_at"}[sort]
        values = ["", "", "A", "B", "B"] if sort == "code" else [None, "", 1, 2, 2]
        if sort == "updated":
            values[0] = ""  # updated_at 为 NOT NULL。
        for i, value in enumerate(values, 1):
            await db._write("INSERT INTO videos(id,slug,created_at,updated_at) VALUES(?,?,1,1)", (i, f"abc-{i}"))
            await db._write(f"UPDATE videos SET {column}=? WHERE id=?", (value, i))
        query = VideoQuery(sort=sort, desc=desc)
        items, total = await db.search_videos(query)
        assert total == 5
        assert [v["id"] for v in items] == ([5, 4, 3, 2, 1] if desc else [3, 4, 5, 1, 2])
        plan = await db._all(f"EXPLAIN QUERY PLAN SELECT * FROM videos ORDER BY {query.order()} LIMIT 50", bulk=True)
        assert any(f"videos_sort_{sort}_{'desc' if desc else 'asc'}" in r["detail"] for r in plan)
        assert all("TEMP B-TREE" not in r["detail"] for r in plan)
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["pause", "state_write_failure"])
def test_prepared_backfill_pages_whole_movies_and_resumes(make_store, boot, monkeypatch, interruption):
    async def run():
        db, store = await make_store()
        for i in range(270):
            key = f"abc-{i:04}"
            item = SourceItem(key=key, code=key.upper(), title=key)
            await db.upsert_item("missav", item, key, store.current.site_rank)
            await db.upsert_item("jable", item, key, store.current.site_rank)
        engine = build(db, store, boot, FakeFetcher("", {}))
        job_id = await engine.create_prepared_job("backfill")
        original, calls = engine.checkpoint, []
        async def pause(jid):
            calls.append(jid)
            if len(calls) == 2:
                await db.update_job(jid, status="paused")
            await original(jid)
        update_job = db.update_job
        async def fail_cursor(jid, **fields):
            if "prepare_before" in fields.get("state", {}):
                raise RuntimeError("interrupted before saving cursor")
            return await update_job(jid, **fields)
        if interruption == "pause":
            monkeypatch.setattr(engine, "checkpoint", pause)
        else:
            monkeypatch.setattr(db, "update_job", fail_cursor)
        with pytest.raises(TaskStopped if interruption == "pause" else RuntimeError):
            await engine._do_prepare(await db.get_job(job_id), {})
        state = (await db.get_job(job_id))["state"]
        if interruption == "pause":
            assert state["queued"] == 256 and state["prepare_before"] is not None
        assert await db.open_task_count(job_id) == 257  # 包括 prepare 本身。
        monkeypatch.setattr(engine, "checkpoint", original)
        monkeypatch.setattr(db, "update_job", update_job)
        await db.update_job(job_id, status="running")
        await engine._do_prepare(await db.get_job(job_id), {})
        tasks = await db.list_tasks(job_id, limit=1000)
        details = [t for t in tasks if t["kind"] == "detail"]
        assert len(details) == 270 and len({t["target"] for t in details}) == 270
        assert all(t["site"] == "jable" for t in details)
        assert (await db.get_job(job_id))["state"]["queued"] == 270
    asyncio.run(run())


def test_quality_and_probe_pages_do_not_stop_on_disabled_sites(make_store, boot):
    async def run():
        db, store = await make_store()
        # 第一批最高 id 全是禁用站点，仍应继续查询较早的启用站点。
        await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"), "abc-001", store.current.site_rank)
        for i in range(260):
            key = f"xyz-{i:04}"
            await db.upsert_detail("missav", SourceDetail(key=key, code=key.upper(), title=key), key, store.current.site_rank)
        store.current.sites["missav"].enabled = False
        engine = build(db, store, boot, FakeFetcher("", {}))
        for kind in ("quality", "probe"):
            job_id = await engine.create_prepared_job(kind, site="javguru")
            await engine._do_prepare(await db.get_job(job_id), {})
            tasks = await db.list_tasks(job_id, limit=1000)
            prepared = [t for t in tasks if t["kind"] != "prepare"]
            assert len(prepared) == (1 if kind == "quality" else 261)
            assert (await db.get_job(job_id))["state"]["queued"] == len(prepared)
    asyncio.run(run())


def test_open_task_existence_and_queue_statistics_keep_status_semantics(make_store):
    async def run():
        db, _ = await make_store()
        running = await db.create_job("quality", "running", {})
        paused = await db.create_job("quality", "paused", {}, status="paused")
        done = await db.create_job("quality", "done", {}, status="done")
        assert not await db.has_open_tasks(running)
        for job in (running, paused, done):
            await db.add_tasks(job, "quality", ["one"])
        assert await db.has_open_tasks(running)
        assert await db.queue_stats() == {"quality": {"pending": 2}}
        task = await db.claim_task()
        assert task["job_id"] == running and await db.has_open_tasks(running)
        await db.finish_task(task["id"], "failed")
        assert not await db.has_open_tasks(running)
        assert await db.queue_stats() == {"quality": {"failed": 1, "pending": 1}}
    asyncio.run(run())
