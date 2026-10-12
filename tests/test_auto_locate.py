import asyncio
from pathlib import Path

from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job


def test_auto_locate_tracks_mdcng_moves_without_rewriting(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        fetcher = FakeFetcher(html, ids)
        engine = Engine(db, fetcher, OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        try:
            await engine._missing_task
            external = tmp_path / "mdc"
            lib_id = (await engine.create_library("外部", "收件", external_dir=str(external)))["id"]
            await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1,
                                                       detail=False, library_id=lib_id))
            video = await db.get_video("ipzz-983")
            original = await db.get_output(video["id"], lib_id)
            other_id = (await engine.create_library("外部2", "收件2", external_dir=str(tmp_path / "mdc2")))["id"]
            await db.ensure_output(video["id"], other_id)
            await engine._output_one(video, other_id, cover=False)
            # 同一自动任务包含两个库，并发扫描的结果需要完整累加。
            other_source = (await db.get_output(video["id"], other_id))["strm_path"]
            other_moved = tmp_path / "mdc2" / "整理好.strm"
            other_moved.parent.mkdir()
            Path(other_source).rename(other_moved)
            await engine.strm.index(engine.libs[lib_id])  # 先缓存旧位置，模拟整理发生在扫描之后
            moved = external / "演员" / "改名.strm"
            moved.parent.mkdir(parents=True)
            source = engine.writer.library_root(engine.libs[lib_id]) / "IPZZ-983" / "IPZZ-983.strm"
            content = source.read_bytes()
            source.rename(moved)
            nfo = moved.with_suffix(".nfo")
            poster = moved.with_name("poster.jpg")
            nfo.write_bytes(b"mdcng nfo")
            poster.write_bytes(b"mdcng poster")
            calls = list(fetcher.calls)
            assert original["strm_path"] == str(source)

            # 首次调度即可同步，未到下个间隔不会重复排任务。
            await engine._run_due_locate()
            job = (await db.list_jobs(kind="locate"))[0]
            assert job["params"]["automatic"] is True
            job = await wait_job(db, job["id"])
            assert job["state"]["checked"] == 25
            assert job["state"]["updated"] == 2 and job["state"]["missing"] == 0
            assert (await db.get_output(video["id"], lib_id))["strm_path"] == str(moved)
            assert (await db.get_output(video["id"], other_id))["strm_path"] == str(other_moved)
            await engine._run_due_locate()
            assert len(await db.list_jobs(kind="locate")) == 1

            # MDCNG 再次改目录、改名，下个周期能找到新位置。
            moved2 = external / "再次改名.strm"
            moved.rename(moved2)
            await db._write("UPDATE system_schedules SET last_run_at=last_run_at-interval*60 WHERE key='external_locate'")
            await engine._run_due_locate()
            await wait_job(db, (await db.list_jobs(kind="locate"))[0]["id"])
            assert (await db.get_output(video["id"], lib_id))["strm_path"] == str(moved2)

            # 整理目录离线不补回、不清空历史路径；恢复挂载后下轮刷新缺失提示。
            offline = tmp_path / "offline"
            external.rename(offline)
            await db._write("UPDATE system_schedules SET last_run_at=last_run_at-interval*60 WHERE key='external_locate'")
            await engine._run_due_locate()
            job = await wait_job(db, (await db.list_jobs(kind="locate"))[0]["id"])
            assert job["state"]["missing"] == 1 and engine.missing[lib_id] == 1
            assert (await db.get_output(video["id"], lib_id))["strm_path"] == str(moved2)
            assert not source.exists()
            offline.rename(external)
            await db._write("UPDATE system_schedules SET last_run_at=last_run_at-interval*60 WHERE key='external_locate'")
            await engine._run_due_locate()
            await wait_job(db, (await db.list_jobs(kind="locate"))[0]["id"])
            assert engine.missing[lib_id] == 0
            assert moved2.read_bytes() == content
            assert nfo.read_bytes() == b"mdcng nfo" and poster.read_bytes() == b"mdcng poster"
            assert fetcher.calls == calls
        finally:
            await engine.stop()

    asyncio.run(run())


def test_auto_locate_respects_settings_pause_and_active_jobs(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        await engine.reload_libraries()
        await engine._run_due_locate()
        assert await db.list_jobs() == []  # 普通库不扫描
        await engine.create_library("外部", "收件", external_dir=str(tmp_path / "mdc"))
        await store.update({"external_sync_interval": 0})
        await engine._run_due_locate()
        assert await db.list_jobs() == []
        await store.update({"external_sync_interval": 2})
        engine.paused = True
        await engine._run_due_locate()
        assert await db.list_jobs() == []
        engine.paused = False
        manual = await engine.strm.create_locate()
        await engine._run_due_locate()
        assert len(await db.list_jobs()) == 1
        await db.update_job(manual, status="paused")
        await engine._run_due_locate()
        assert len(await db.list_jobs()) == 1
        await db.update_job(manual, status="done")
        await engine._run_due_locate()
        assert len(await db.list_jobs()) == 2
        auto = (await db.list_jobs(kind="locate"))[0]
        await db.update_job(auto["id"], status="done")
        await db._write("UPDATE system_schedules SET last_run_at=last_run_at-119,schedule_updated_at=schedule_updated_at-119 WHERE key='external_locate'")
        await engine._run_due_locate()
        assert len(await db.list_jobs()) == 2
        await db._write("UPDATE system_schedules SET last_run_at=last_run_at-1,schedule_updated_at=schedule_updated_at-1 WHERE key='external_locate'")
        await engine._run_due_locate()
        assert len(await db.list_jobs()) == 3

    asyncio.run(run())


def test_auto_locate_skips_pending_library_changes(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        lib_id = (await engine.create_library("外部", "收件", external_dir=str(tmp_path / "mdc")))["id"]
        lib = engine.libs[lib_id]
        lib["operation_pending"] = 1
        await engine._run_due_locate()
        assert await db.list_jobs() == []
        other_id = (await engine.create_library("外部2", "收件2", external_dir=str(tmp_path / "mdc2")))["id"]
        engine.libs[lib_id]["operation_pending"] = 1
        await engine._run_due_locate()
        job = (await db.list_jobs(kind="locate"))[0]
        tasks = await db._all("SELECT * FROM tasks WHERE job_id=?", (job["id"],))
        assert [int(task["target"]) for task in tasks] == [other_id]
        # 排队之后库变更进入待处理，也必须跳过，不去扫描旧目录。
        engine.libs[other_id]["operation_pending"] = 1
        await engine.strm.do_locate(job, tasks[0])
        assert (await db.get_job(job["id"]))["state"] == {}
        assert other_id not in engine.strm._index

    asyncio.run(run())
