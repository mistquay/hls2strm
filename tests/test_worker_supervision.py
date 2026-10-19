import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hls2strm import engine as engine_module, workers
from .conftest import FakeFetcher
from .test_auth import _client, _cleanup
from .test_engine import build, wait_job


async def single_site(store, count=1):
    await store.update({"sites": {name: {"enabled": name == "jable", "concurrency": count}
                                 for name in store.current.sites}})


@pytest.mark.parametrize("failure", ["claim", "before_done", "after_done"])
def test_worker_recovers_without_replaying_finished_tasks(make_store, boot, monkeypatch, failure):
    monkeypatch.setattr(workers, "RETRY_DELAY", .01)

    async def run():
        db, store = await make_store()
        await single_site(store)
        engine = build(db, store, boot, FakeFetcher("", {}))
        seen, failed = [], False

        async def handler(job, task):
            seen.append(task["target"])

        monkeypatch.setattr(engine, "_do_probe", handler)
        original_claim, original_run = db.claim_task, engine._run

        async def flaky_claim(*args, **kwargs):
            nonlocal failed
            if failure == "claim" and not failed:
                failed = True
                raise sqlite3.OperationalError("injected transient claim failure")
            return await original_claim(*args, **kwargs)

        async def flaky_run(task):
            nonlocal failed
            if failure == "before_done" and not failed:
                failed = True
                raise OSError("injected outer task failure")
            await original_run(task)
            if failure == "after_done" and not failed:
                failed = True
                raise OSError("injected finalization failure")

        monkeypatch.setattr(db, "claim_task", flaky_claim)
        monkeypatch.setattr(engine, "_run", flaky_run)
        job = await db.create_job("probe", "supervision", {})
        await db.add_tasks(job, "probe", ["first", "second"], site="jable")
        await engine.start()
        try:
            await wait_job(db, job, timeout=5)
            assert failed and seen == ["first", "second"]
            assert await db.task_counts(job) == {"done": 2}
            assert all(t["attempts"] == 1 for t in await db.list_tasks(job))
            assert all(not task.done() for task in engine._workers)
            assert engine.metrics.counters["worker_errors"] == 1
            assert not engine.running
        finally:
            await engine.stop()
    asyncio.run(run())


def test_worker_pool_replaces_slots_and_handles_resize(make_store, boot):
    async def run():
        db, store = await make_store()
        await single_site(store, 3)
        engine = build(db, store, boot, FakeFetcher("", {}))
        engine.pause()
        engine._resize_workers()
        try:
            first, second, third = engine._workers
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            engine._resize_workers()
            assert engine._workers[0] is not first
            assert engine._workers[1:] == [second, third]
            assert [task.get_name() for task in engine._workers] == [f"worker-{i}" for i in range(3)]
            await store.update({"sites": {"jable": {"concurrency": 2}}})
            engine.notify()
            await asyncio.wait_for(third, 3)
            await store.update({"sites": {"jable": {"concurrency": 3}}})
            engine._resize_workers()
            assert engine._workers[2] is not third
            assert len({task.get_name() for task in engine._workers}) == 3
        finally:
            await engine.stop()
    asyncio.run(run())


def test_scheduler_rebuilds_exited_worker_and_readiness_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "SCHEDULER_INTERVAL", .01)
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            async def replace_worker():
                engine = app.state.ctx.engine
                worker = engine._workers[0]
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                async with asyncio.timeout(2):
                    while engine._workers[0] is worker:
                        await asyncio.sleep(.01)
                return engine._workers[0].get_name()
            assert client.portal.call(replace_worker) == "worker-0"
            assert client.get("/readyz").status_code == 200
    finally:
        _cleanup()


def test_container_healthcheck_uses_readiness_and_detects_worker_loss(tmp_path):
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8080/readyz" in dockerfile
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            async def cancel_worker():
                worker = app.state.ctx.engine._workers[0]
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            client.portal.call(cancel_worker)
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
    finally:
        _cleanup()
