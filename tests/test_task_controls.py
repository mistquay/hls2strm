import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.db import Database
from hls2strm.job_views import details
from hls2strm.task_controls import cancel_pending

from .conftest import FakeFetcher
from .test_engine import build


def test_cancel_scope_preserves_other_sites_running_tasks_and_workflow_steps(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        jid = await db.create_job("probe", "mixed", {}, status="paused")
        await db.add_tasks(jid, "probe", ["123av:1", "123av:2", "123av:3", "123av:4"], site="123av")
        await db.add_tasks(jid, "probe", ["missav:1"], site="missav")
        await db.add_tasks(jid, "detail", ["abc-001"], site="123av")
        await db.add_tasks(jid, "prepare", ["all"])
        await db._write("UPDATE tasks SET status='running',attempts=1 WHERE target='123av:3'")
        await db._write("UPDATE tasks SET status='done',result='{\"sources_added\":1}' WHERE target='123av:4'")
        await db._write("UPDATE tasks SET next_run_at=? WHERE status='pending'", (int(time.time()) + 300,))
        assert await cancel_pending(engine, jid, site="123av", kind="probe") == 2
        assert await cancel_pending(engine, jid, site="123av", kind="probe") == 0
        rows = {r["target"]: r for r in await db.list_tasks(jid)}
        assert rows["123av:1"]["status"] == rows["123av:2"]["status"] == "cancelled"
        assert rows["123av:1"]["next_run_at"] == 0
        assert rows["123av:3"]["status"] == "running" and rows["123av:3"]["attempts"] == 1
        assert rows["123av:4"]["status"] == "done" and rows["123av:4"]["result"] == '{"sources_added":1}'
        assert rows["missav:1"]["status"] == rows["abc-001"]["status"] == rows["all"]["status"] == "pending"
        assert (await db.get_job(jid))["status"] == "paused"
        assert await cancel_pending(engine, jid) == 2
        assert (await db.list_tasks(jid, "pending"))[0]["kind"] == "prepare"
    asyncio.run(run())


@pytest.mark.parametrize("status", ["running", "paused"])
def test_cancelling_last_probe_finishes_parent_and_survives_restart(make_store, boot, status):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        jid = await db.create_job("crawl", "MissAV", {"site": "missav"}, status=status)
        await db.add_tasks(jid, "list", [1], site="missav")
        await db._write("UPDATE tasks SET status='done' WHERE job_id=?", (jid,))
        await db.update_job(jid, state={"list_complete": True})
        await db.add_tasks(jid, "probe", ["123av:1", "123av:2"], site="123av")
        output = boot.data_dir / "existing.strm"
        output.write_text("http://example/play/abc-001\n", encoding="utf-8")
        assert await cancel_pending(engine, jid, kind="probe") == 2
        job = await details(engine, await db.get_job(jid))
        assert job["status"] == "done" and job["finished_at"] is not None
        assert job["phase"] == "partial_cancelled"
        assert job["state"]["list_complete"]
        assert job["tasks"] == {"done": 1, "cancelled": 2}
        assert not job["pending_groups"]
        assert output.read_text(encoding="utf-8") == "http://example/play/abc-001\n"
        assert await cancel_pending(engine, jid) == 0
        await db.close()
        reopened = Database(boot.data_dir / "test.db")
        await reopened.open()
        try:
            assert await reopened.reset_running_tasks() == 0
            assert await reopened.task_counts(jid) == {"done": 1, "cancelled": 2}
        finally:
            await reopened.close()
    asyncio.run(run())


def test_cancelling_unfinished_lists_does_not_claim_full_scan(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        jid = await db.create_job("crawl", "MissAV", {"site": "missav"})
        await db.add_tasks(jid, "list", [1, 2], site="missav")
        first = await db.claim_task()
        await db.finish_task(first["id"], "done")
        assert await cancel_pending(engine, jid, kind="list") == 1
        job = await db.get_job(jid)
        assert job["status"] == "done" and job["state"]["partial_failure"]
        assert not job["state"]["list_complete"]
    asyncio.run(run())


def test_worker_claimed_task_finishes_after_pending_cancellation(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(job, task):
            entered.set()
            await release.wait()
            return {"sources_added": 1}
        monkeypatch.setattr(engine, "_do_probe", handler)
        jid = await db.create_job("probe", "in flight", {})
        await db.add_tasks(jid, "probe", ["123av:1", "123av:2"], site="123av")
        task = await db.claim_task()
        engine.running[task["id"]] = {"site": "123av"}
        work = asyncio.create_task(engine._run(task))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert await cancel_pending(engine, jid) == 1
            assert (await db.get_job(jid))["status"] == "running"
            release.set()
            await asyncio.wait_for(work, 2)
            assert await db.task_counts(jid) == {"done": 1, "cancelled": 1}
            assert (await details(engine, await db.get_job(jid)))["results"] == {"sources_added": 1}
        finally:
            release.set()
            await work
    asyncio.run(run())


def test_api_cancel_single_task_is_authenticated_scoped_and_validated(tmp_path):
    with TestClient(create_app(BootConfig(data_dir=tmp_path, ui_password="test-password"))) as client:
        ctx = client.app.state.ctx
        async def seed():
            ctx.engine.pause()
            job = await ctx.db.create_job("probe", "selected", {}, status="paused")
            other = await ctx.db.create_job("probe", "other", {}, status="paused")
            await ctx.db.add_tasks(job, "probe", ["123av:1", "missav:1"], site="123av")
            await ctx.db.add_tasks(other, "probe", ["123av:2"], site="123av")
            rows = await ctx.db.list_tasks(job)
            return job, other, next(t["id"] for t in rows if t["target"] == "123av:1"), (await ctx.db.list_tasks(other))[0]["id"]
        jid, other, tid, outside = client.portal.call(seed)
        url = f"/api/jobs/{jid}/tasks/cancel-pending"
        assert client.post(url, json={"task_ids": [tid]}).status_code == 401
        client.auth = ("admin", "test-password")
        assert client.post(url, json={"task_ids": [tid, outside]}).json() == {"cancelled": 1}
        assert client.post(url, json={"task_ids": [tid]}).json() == {"cancelled": 0}
        assert client.get(f"/api/jobs/{other}").json()["tasks"] == {"pending": 1}
        for body in ({"kind": "prepare"}, {"site": "invalid"}, {"task_ids": []}, {"task_ids": [0]}, {"task_ids": [1] * 501}):
            assert client.post(url, json=body).status_code == 422
        assert client.post("/api/jobs/999999/tasks/cancel-pending", json={}).status_code == 404
        assert client.post(url, json={}).json() == {"cancelled": 1}
        result = client.get(f"/api/jobs/{jid}").json()
        assert result["status"] == "done" and result["phase"] == "partial_cancelled"
        assert result["tasks"] == {"cancelled": 2}
