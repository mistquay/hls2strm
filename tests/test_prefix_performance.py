"""分页去重、快照发布与播放资源隔离的行为回归。"""

import asyncio
import hashlib
import threading

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig, SettingsStore
from hls2strm.db import Database, VideoQuery
from hls2strm.engine import TaskStopped
from hls2strm.job_views import details
from hls2strm.prefix_crawl import create, cancel
from hls2strm.prefix_snapshot import build as build_snapshot
from hls2strm.prefix_store import progress_page, progress_summary, snapshot, statistics
from .conftest import FakeFetcher
from .test_engine import build, wait_job
from .test_prefix_crawl import body


async def seed(db, count=300):
    await db._write_many(
        "INSERT INTO videos(id,slug,code,code_key,created_at,updated_at) VALUES(?,?,?,?,1,1)",
        [(i, f"abc-{i:04}", f"ABC-{i:04}", f"ABC-{i}") for i in range(1, count+1)])


def test_snapshot_keeps_stable_membership_without_holding_playback_writer(make_store):
    async def run():
        db, _ = await make_store()
        await seed(db)
        entered, release = asyncio.Event(), asyncio.Event()
        scope = None

        async def on_batch(scope_id, processed, total):
            nonlocal scope
            scope = scope_id
            if processed == 256:
                assert total == 300
                entered.set()
                await release.wait()

        task = asyncio.create_task(build_snapshot(db, {}, on_batch=on_batch))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not db._lock.locked()
            with pytest.raises(ValueError):
                await statistics(db, scope, "missav")
            await asyncio.wait_for(db._write(
                "INSERT INTO play_sessions(id,source_id,data,expires_at) VALUES('player',1,'{}',100)"), 1)
            await db._write("UPDATE videos SET code='NEW-0300',code_key='NEW300' WHERE id=300")
            await db._write("INSERT INTO videos(id,slug,code,code_key,created_at,updated_at) VALUES(301,'new-0301','NEW-0301','NEW301',1,1)")
        finally:
            release.set()
        assert await task == scope
        rows = await db._all("SELECT video_id,code FROM prefix_scope_videos WHERE scope_id=? ORDER BY video_id", (scope,))
        assert len(rows) == 300 and rows[-1]["code"] == "ABC-0300"
        fingerprint = hashlib.sha256(b"".join(str(i).encode()+b"," for i in range(1,301))).hexdigest()
        assert (await db._one("SELECT fingerprint FROM prefix_scopes WHERE id=? AND ready=1", (scope,)))[0] == fingerprint
        assert (await statistics(db, scope, "missav"))["total"] == 300
        assert (await statistics(db, scope, "missav"))["items"][0]["prefix"] == "ABC"
        assert await build_snapshot(db, {}, scope_id=scope) == scope
        assert (await statistics(db, scope, "missav"))["total"] == 300
    asyncio.run(run())


def test_incomplete_snapshot_survives_restart_and_is_not_usable(make_store, boot):
    async def run():
        db, store = await make_store()
        await seed(db)
        scope = None

        async def interrupt(scope_id, processed, total):
            nonlocal scope
            scope = scope_id
            if processed == 256:
                raise TaskStopped("pending")

        with pytest.raises(TaskStopped):
            await build_snapshot(db, {}, on_batch=interrupt)
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()
        with pytest.raises(ValueError):
            await create(engine, body(scope))
        await db.close()
        db = Database(boot.data_dir / "test.db")
        await db.open()
        try:
            with pytest.raises(ValueError):
                await statistics(db, scope, "missav")
            assert await build_snapshot(db, {}, scope_id=scope) == scope
            assert (await statistics(db, scope, "missav"))["total"] == 300
            assert (await db._one("SELECT COUNT(*) FROM prefix_scope_videos WHERE scope_id=?", (scope,)))[0] == 300
        finally:
            await db.close()
    asyncio.run(run())


def test_prefix_page_has_stable_filtering_and_global_distinct_counts(make_store, boot):
    async def run():
        db, store = await make_store()
        await seed(db, 2)
        scope = await snapshot(db, {})
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()
        job = await create(engine, body(scope, prefixes=["ABC", "DEF", "GHI"]))
        await db._write_many("INSERT INTO prefix_hits(job_id,site,source_key,prefix,page,video_id,new_video,new_source,outcome) VALUES(?,?,?,?,1,?,1,1,'accepted')", [
            (job,"missav","one","ABC",1), (job,"missav","two","DEF",1), (job,"missav","three","GHI",2)])
        first = await progress_page(db, job, limit=2)
        assert first["total"] == 3 and [r["prefix"] for r in first["items"]] == ["ABC","DEF"]
        assert all(r["pending"] == 1 and r["sources_added"] == 1 for r in first["items"])
        second = await progress_page(db, job, limit=2, offset=2)
        assert [r["prefix"] for r in second["items"]] == ["GHI"]
        assert [r["prefix"] for r in (await progress_page(db, job, q="de"))["items"]] == ["DEF"]
        assert (await progress_page(db, job, q="%"))["total"] == 0
        summary = await progress_summary(db, job)
        assert summary == {"seen":2,"new":2,"sources_added":3,"skipped":0}
        await cancel(engine, job, "missav", "ABC")
        assert (await progress_page(db, job, q="abc"))["items"][0]["scan_status"] == "cancelled"
    asyncio.run(run())


def test_prefix_summary_payload_omits_runs_and_invalidates_on_control(make_store, boot):
    async def run():
        db, store = await make_store()
        scope = await snapshot(db, {})
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()
        job = await create(engine, body(scope))
        result = await details(engine, await db.get_job(job), summary_only=True)
        assert "prefix_progress" not in result and result["tasks"] == {"pending":1}
        await engine.set_job_status(job, "cancel")
        result = await details(engine, await db.get_job(job), summary_only=True)
        assert result["status"] == "cancelled" and result["tasks"] == {"cancelled":1}
    asyncio.run(run())


def test_video_browse_does_not_wait_for_bulk_statistics(make_store):
    async def run():
        db, _ = await make_store()
        await seed(db, 1)
        entered, release = threading.Event(), threading.Event()
        def block(sql):
            if sql.startswith("SELECT"):
                entered.set()
                release.wait(3)
        await db.bulk_reader.set_trace_callback(block)
        task = asyncio.create_task(db._video_stats())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            rows, total = await asyncio.wait_for(db.search_videos(VideoQuery()), 1)
            assert total == 1 and rows[0]["slug"] == "abc-0001"
            assert not task.done()
        finally:
            release.set()
            await task
            await db.bulk_reader.set_trace_callback(None)
    asyncio.run(run())


def test_background_stats_api_is_durable_idempotent_and_cancellable(tmp_path):
    with TestClient(create_app(BootConfig(data_dir=tmp_path))) as client:
        ctx = client.app.state.ctx
        client.portal.call(ctx.store.update, {"health_interval":0})
        ctx.engine.pause()
        client.portal.call(seed, ctx.db, 300)
        headers={"Idempotency-Key":"background-stats"}
        request={"background":True,"site":"missav"}
        response=client.post("/api/prefixes/stats",json=request,headers=headers)
        assert response.status_code == 202
        job_id=response.json()["job_id"]
        assert client.post("/api/prefixes/stats",json=request,headers=headers).json() == response.json()
        assert client.get(f"/api/jobs/{job_id}?summary=true").json()["phase"] == "scheduler_paused"
        ctx.engine.resume()
        job=client.portal.call(wait_job,ctx.db,job_id)
        assert job["state"]["processed"] == 300
        scope=job["params"]["scope_id"]
        assert client.post("/api/prefixes/stats",json={"scope_id":scope,"site":"missav"}).json()["total"] == 300
        ctx.engine.pause()
        other=client.post("/api/prefixes/stats",json={"background":True}).json()["job_id"]
        assert client.post(f"/api/jobs/{other}/cancel").status_code == 200
        assert client.get(f"/api/jobs/{other}?summary=true").json()["status"] == "cancelled"
        assert client.post("/api/prefixes/stats",json={"background":True,"query":{"unknown":True}}).status_code == 400


def test_v25_migration_preserves_published_scopes(make_store, boot, monkeypatch):
    from hls2strm import db as db_module
    monkeypatch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:26])
    async def run():
        db, _ = await make_store()
        scope = await snapshot(db, {})
        await db.conn.execute("ALTER TABLE prefix_scopes DROP COLUMN ready")
        for name in ("tasks_job_recent","tasks_job_status_recent","crawl_items_page","tasks_prefix_progress"):
            await db.conn.execute(f"DROP INDEX {name}")
        await db.conn.execute("PRAGMA user_version=25")
        await db.close()
        db=Database(boot.data_dir/"test.db")
        await db.open()
        try:
            from hls2strm.db import MIGRATIONS
            assert (await db._one("PRAGMA user_version"))[0] == len(MIGRATIONS)
            assert (await statistics(db,scope,"missav"))["total"] == 0
        finally:
            await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "cancel", "restart"])
def test_running_snapshot_control_and_worker_recovery(make_store, boot, monkeypatch, action):
    from hls2strm import prefix_snapshot
    original = prefix_snapshot.build
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_build(*args, **kwargs):
        callback = kwargs["on_batch"]
        async def on_batch(scope, processed, total):
            await callback(scope, processed, total)
            if processed == 256:
                entered.set()
                await release.wait()
        kwargs["on_batch"] = on_batch
        return await original(*args, **kwargs)
    monkeypatch.setattr(prefix_snapshot, "build", slow_build)

    async def run():
        db, store = await make_store()
        await seed(db)
        fetcher = FakeFetcher("", {})
        engine = build(db, store, boot, fetcher)
        await engine.start()
        reopened = False
        try:
            jid = await prefix_snapshot.create(engine, {"query":{},"ids":None,"scope_id":None,"site":"missav"})
            scope = (await db.get_job(jid))["params"]["scope_id"]
            await asyncio.wait_for(entered.wait(), 3)
            if action == "restart":
                await engine.stop()
                await db.close()
                db = Database(boot.data_dir/"test.db")
                await db.open()
                reopened = True
                store = SettingsStore(boot, db)
                await store.load()
                engine = build(db, store, boot, fetcher)
            else:
                await engine.set_job_status(jid, action)
            assert (await db._one("SELECT ready FROM prefix_scopes WHERE id=?", (scope,)))[0] == 0
            release.set()
            if action == "restart":
                await engine.start()
            else:
                async with asyncio.timeout(3):
                    while (await db._one("SELECT status FROM tasks WHERE job_id=?", (jid,)))[0] == "running":
                        await asyncio.sleep(.01)
            if action == "cancel":
                assert (await db.get_job(jid))["status"] == "cancelled"
                with pytest.raises(ValueError):
                    await statistics(db, scope, "missav")
            else:
                if action == "pause":
                    await engine.set_job_status(jid, "resume")
                job = await wait_job(db, jid)
                assert not job["state"].get("partial_failure") and job["state"]["processed"] == 300
                assert (await statistics(db, scope, "missav"))["total"] == 300
        finally:
            release.set()
            await engine.stop()
            if reopened:
                await db.close()
    asyncio.run(run())
