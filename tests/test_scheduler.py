import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from hls2strm.api import router
from hls2strm.db import Database, MIGRATIONS
from hls2strm.schedule import ScheduleFields, preview
from hls2strm.scheduler import Scheduler
from .test_config_transfer import engine_for


def test_shared_plan_preview_and_validation():
    current = datetime(2026, 10, 12, 2, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert preview(ScheduleFields(interval=90), current, 3) == [int(current + 5400 * n) for n in (1, 2, 3)]
    times = preview(ScheduleFields(cron="15 3,15 * * *"), current, 3)
    assert [datetime.fromtimestamp(t, ZoneInfo("Asia/Shanghai")).hour for t in times] == [3, 15, 3]
    assert preview(ScheduleFields(cron=""), current) == []
    for fields in ({"cron": "* * * * * *"}, {"cron": "0 0 31 2 *"}, {"interval": -1}, {"timezone": "Mars/City"}):
        with pytest.raises(ValidationError):
            ScheduleFields(**fields)


def test_migration_preserves_existing_settings_and_plan_edits(tmp_path):
    async def run():
        path = tmp_path / "old.db"
        async with aiosqlite.connect(path, isolation_level=None) as conn:
            for step in MIGRATIONS[:20]:
                await step(conn)
            await conn.execute("PRAGMA user_version=20")
            await conn.execute("INSERT INTO settings(key,value) VALUES('settings',?)",
                               (json.dumps({"external_sync_interval": 90, "health_interval": 0, "orphan_cleanup": False}),))
            before = await conn.execute_fetchall("SELECT id,interval,cron,enabled,initialized FROM subscriptions")
        db = Database(path)
        await db.open()
        plans = {r["key"]: dict(r) for r in await db._all("SELECT * FROM system_schedules")}
        assert plans["external_locate"]["interval"] == 90
        assert not plans["health"]["enabled"] and not plans["orphans"]["enabled"]
        after = await db._all("SELECT id,interval,cron,enabled,initialized FROM subscriptions")
        assert [tuple(r) for r in after] == before
        await db._write("UPDATE system_schedules SET cron='15 4 * * *',interval=0 WHERE key='external_locate'")
        await db.close()
        await db.open()
        assert (await db._one("SELECT cron FROM system_schedules WHERE key='external_locate'"))["cron"] == "15 4 * * *"
        assert (await db._one("PRAGMA user_version"))[0] == len(MIGRATIONS)
        await db.close()
    asyncio.run(run())


def test_schedule_api_read_preview_edit_and_idempotent_execution(make_store, boot):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        lib = (await engine.create_library("外部", "inbox", external_dir="organized"))["id"]
        sub_id = await db.create_subscription(name="原订阅", site="jable", source="/latest-updates/", library_id=lib,
                                              cron="15 3 * * *", initialized=1, enabled=1, last_run_at=123)
        app = FastAPI()
        app.state.ctx = SimpleNamespace(engine=engine, db=db, store=store, auth=SimpleNamespace(required=False))
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            before = await db._all("SELECT * FROM system_schedules")
            for _ in range(2):
                response = await client.get("/api/schedules")
                assert response.status_code == 200
                assert len(response.json()) == 6 + len(await db.list_subscriptions())
                assert (await client.get("/api/schedules/system:trash/history")).json()["total"] == 0
                assert len((await client.post("/api/schedules/preview", json={"cron": None, "interval": 90})).json()["times"]) == 5
            assert [tuple(r) for r in before] == [tuple(r) for r in await db._all("SELECT * FROM system_schedules")]
            assert await db.list_jobs() == [] and not store.output_dir.exists()
            assert (await client.post("/api/schedules/system:trash/run", json={})).status_code == 400
            assert (await client.put("/api/schedules/system:health", json={"cron": "bad"})).status_code == 422
            assert (await client.put("/api/schedules/system:unknown", json={"cron": "", "interval": 0})).status_code == 404
            assert (await client.put("/api/schedules/system:health", json={"cron": "", "command": "anything"})).status_code == 422
            patch = {"cron": None, "interval": 90, "timezone": "Asia/Shanghai", "enabled": True}
            assert (await client.put(f"/api/schedules/subscription:{sub_id}", json=patch)).status_code == 200
            sub = await db.get_subscription(sub_id)
            assert sub["id"] == sub_id and sub["last_run_at"] == 123 and sub["initialized"] == 1
            assert sub["interval"] == 90 and sub["cron"] is None and sub["name"] == "原订阅"
            assert (await client.get("/api/subscriptions")).json()[-1]["interval"] == 90
            assert await db.list_jobs() == []

            headers = {"Idempotency-Key": "manual-locate-once"}
            first = await client.post("/api/schedules/system:external_locate/run", json={}, headers=headers)
            second = await client.post("/api/schedules/system:external_locate/run", json={}, headers=headers)
            assert first.status_code == 200 and second.json() == first.json()
            assert len(await db.list_jobs(kind="locate")) == 1
            assert (await db._one("SELECT priority FROM tasks WHERE job_id=?", (first.json()["job_id"],)))["priority"] == 20
            assert (await client.post("/api/schedules/system:external_locate/run", json={})).status_code == 409
            assert (await client.put("/api/schedules/system:external_locate", json={**patch, "enabled": False})).status_code == 200
            assert (await db.get_job(first.json()["job_id"]))["status"] == "running"
            row = next(r for r in (await client.get("/api/schedules")).json() if r["key"] == "system:external_locate")
            assert row["state"] == "queued" and not row["can_run"] and not row["enabled"]
            history = (await client.get("/api/schedules/system:external_locate/history")).json()
            assert history["total"] == 1 and history["items"][0]["status"] == "queued"
            await db.update_job(first.json()["job_id"], status="done", state={"partial_failure": True}, finished_at=456)
            history = (await client.get("/api/schedules/system:external_locate/history")).json()
            assert history["items"][0]["status"] == "failed"
            await db.delete_job(first.json()["job_id"])
            assert (await client.get("/api/schedules/system:external_locate/history")).json()["items"][0]["status"] == "failed"
            app.state.ctx.auth = SimpleNamespace(required=True, verify=lambda _: None)
            assert (await client.get("/api/schedules")).status_code == 401
            assert (await client.post("/api/schedules/preview", json={})).status_code == 401
            assert (await client.post("/api/schedules/system:trash/run", json={"confirm": True})).status_code == 401
        await engine.scheduler.stop()
    asyncio.run(run())


def test_direct_failure_is_recorded_and_can_be_retried(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        await engine.create_library("外部", "inbox", external_dir="organized")
        def fail(*args):
            raise OSError("read-only filesystem")
        monkeypatch.setattr("hls2strm.scheduler.purge_trash", fail)
        await engine.scheduler.run("system:trash")
        await asyncio.gather(*list(engine.scheduler._tasks.values()))
        history = await engine.scheduler.store.history("system:trash", 10, 0)
        assert history["items"][0]["status"] == "failed"
        assert "read-only filesystem" in history["items"][0]["error"]
        rows = {r["key"]: r for r in await engine.scheduler.list()}
        assert rows["system:trash"]["state"] == "failed" and rows["system:trash"]["can_run"]
        monkeypatch.setattr("hls2strm.scheduler.purge_trash", lambda *args: 2)
        await engine.scheduler.run("system:trash")
        await asyncio.gather(*list(engine.scheduler._tasks.values()))
        history = await engine.scheduler.store.history("system:trash", 10, 0)
        assert history["total"] == 2 and history["items"][0]["result"] == {"removed": 2}
        assert history["items"][0]["status"] == "done"
    asyncio.run(run())


def test_system_cron_and_interval_restart_policies(make_store, boot, monkeypatch):
    clock = [datetime(2026, 10, 12, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    monkeypatch.setattr("hls2strm.scheduler.time.time", lambda: clock[0])
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        await engine.create_library("外部", "inbox", external_dir="organized")
        s = engine.scheduler
        assert await s.next_at(await s.plan("system:external_locate")) == int(clock[0])
        assert await s.next_at(await s.plan("system:health")) == int(clock[0] + 3600)
        patch = {"cron": "15 3 * * *", "interval": 0, "timezone": "Asia/Shanghai", "enabled": True}
        await s.update("system:external_locate", patch)
        planned = await s.next_at(await s.plan("system:external_locate"))
        assert planned > clock[0] and await db.list_jobs() == []
        clock[0] = planned + 30
        await s.due_system("external_locate")
        assert len(await db.list_jobs()) == 1
        job = (await db.list_jobs())[0]
        assert (await db._one("SELECT priority FROM tasks WHERE job_id=?", (job["id"],)))["priority"] == 0
        await db.update_job(job["id"], status="done")
        await s.due_system("external_locate")
        assert len(await db.list_jobs()) == 1
        # 停机错过一天的时刻，重启不能立即补跑。
        clock[0] += 86400 + 3600
        restarted = engine_for(db, store, boot)
        await restarted.reload_libraries()
        await restarted.scheduler.start()
        await restarted.scheduler.due_system("external_locate")
        assert len(await db.list_jobs()) == 1
        assert await restarted.scheduler.next_at(await restarted.scheduler.plan("system:external_locate")) > clock[0]
        await restarted.scheduler.update("system:external_locate", {**patch, "cron": None, "interval": 90})
        assert await restarted.scheduler.next_at(await restarted.scheduler.plan("system:external_locate")) == int(clock[0] + 5400)
    asyncio.run(run())


def test_long_maintenance_does_not_block_tick_and_preserves_pause_scope(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        await engine.create_library("外部", "inbox", external_dir="organized")
        gate = asyncio.Event()
        from hls2strm.play_history import PlaybackHistory
        async def slow_cleanup(self):
            await gate.wait()
        monkeypatch.setattr(PlaybackHistory, "cleanup", slow_cleanup)
        engine.paused = True
        await asyncio.wait_for(engine.scheduler.tick(), timeout=2)
        history = await engine.scheduler.store.history("system:play_history", 10, 0)
        assert history["total"] == 1
        assert await engine.scheduler.store.history("system:external_locate", 10, 0) == {"items": [], "total": 0}
        await asyncio.sleep(0)
        rows = {r["key"]: r for r in await engine.scheduler.list()}
        assert rows["system:play_history"]["state"] == "running"
        assert not rows["system:play_history"]["follows_pause"]
        assert rows["system:external_locate"]["state"] == "paused"
        await engine.scheduler.tick()
        assert (await engine.scheduler.store.history("system:play_history", 10, 0))["total"] == 1
        gate.set()
        await asyncio.gather(*list(engine.scheduler._tasks.values()))
        assert (await engine.scheduler.store.history("system:play_history", 10, 0))["items"][0]["status"] == "done"
    asyncio.run(run())


def test_event_settle_runs_when_fallback_disabled_and_stop_records_interruption(make_store, boot, monkeypatch):
    async def run():
        db, store = await make_store()
        engine = engine_for(db, store, boot)
        await engine.reload_libraries()
        await engine.create_library("派生", "derived", sources=[1], queue=False)
        await engine.scheduler.update("system:settle", {"enabled": False, "cron": "", "interval": 0, "timezone": "Asia/Shanghai"})
        gate = asyncio.Event()
        async def slow_settle():
            engine._settle_wanted = False
            await gate.wait()
            return {"written": 2}
        monkeypatch.setattr(engine, "settle", slow_settle)
        await engine.scheduler.tick()
        await asyncio.sleep(0)
        assert (await engine.scheduler.store.history("system:settle", 10, 0))["total"] == 1
        assert (await engine.scheduler.store.history("system:settle", 10, 0))["items"][0]["trigger"] == "event"
        await engine.scheduler.stop()
        assert (await engine.scheduler.store.history("system:settle", 10, 0))["items"][0]["status"] == "interrupted"
        orphaned = await engine.scheduler.store.begin("system:health", "schedule", None)
        queued = await engine.scheduler.store.begin("system:external_locate", "schedule", None)
        job_id = await db.create_job("locate", "旧排队任务", {})
        await engine.scheduler.store.attach(queued, job_id)
        restarted = Scheduler(engine)
        await restarted.start()
        assert (await db._one("SELECT status FROM schedule_runs WHERE id=?", (orphaned,)))["status"] == "interrupted"
        assert (await db._one("SELECT status FROM schedule_runs WHERE id=?", (queued,)))["status"] == "queued"
    asyncio.run(run())
