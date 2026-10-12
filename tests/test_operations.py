import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.sites import SourceDetail
from hls2strm import output_sync
from hls2strm.file_changes import FileChanges
from .conftest import FakeFetcher
from .test_engine import build, wait_job


async def seed(db,store,key,category="drama",actor="m1"):
    return await db.upsert_detail("jable",SourceDetail(key=key,code=key.upper(),title=key,categories=[{"slug":category,"name":category}],models=[{"id":actor,"name":actor}]),key,store.current.site_rank)


async def prepare(engine,request):
    job_id = await engine.operations.create(request)
    job = await wait_job(engine.db,job_id)
    assert not job["state"].get("partial_failure"),await engine.db.list_tasks(job_id)
    return job


def test_selection_aliases_frozen_targets_and_duplicate_confirmation(make_store,boot):
    async def run():
        db,store = await make_store()
        await store.update({"download_cover":False,"quality_capture":False})
        first = await seed(db,store,"abc-001",actor="m1")
        other = await seed(db,store,"abc-002",actor="m2")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            lid = (await engine.create_library("目标","target"))["id"]
            preview = await prepare(engine,{"kind":"selection","payload":{"action":"add","library_id":lid,"query":{"model":["m1"]}}})
            assert preview["state"]["_preview"]["counts"]["matched"]==1
            late = await seed(db,store,"abc-003",actor="m1")
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            assert await engine.operations.approve(preview["id"],preview["state"]["preview_token"])==job
            result = await wait_job(db,job)
            assert not result["state"].get("partial_failure"),await db.list_tasks(job)
            assert await db.get_output(first,lid)
            assert await db.get_output(other,lid) is None and await db.get_output(late,lid) is None
            assert result["state"]["matched"]==1
        finally:
            await engine.stop()
    asyncio.run(run())


def test_library_preview_is_read_only_and_applies_exact_effects(make_store,boot):
    async def run():
        db,store = await make_store()
        await store.update({"download_cover":False,"quality_capture":False})
        comedy = await seed(db,store,"abc-001","comedy")
        drama = await seed(db,store,"abc-002","drama")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            lib = await engine.create_library("规则","rule",rule={"categories":["comedy"]})
            await wait_job(db,lib["reclassify_job_id"])
            old = await db.get_library(lib["id"])
            original_path = Path((await db.get_output(comedy,lib["id"]))["strm_path"])
            body = {k:old[k] for k in ("name","dir","external_dir","path_template","versions","rule","sources","excludes")}
            body["rule"] = {"categories":["drama"]}
            preview = await prepare(engine,{"kind":"library","payload":{"library_id":lib["id"],"values":body}})
            counts = preview["state"]["_preview"]["counts"]
            assert (counts["added"],counts["removed"])==(1,1)
            assert (await db.get_library(lib["id"]))["rule"]==old["rule"] and original_path.exists()
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            result = await wait_job(db,job)
            assert not result["state"].get("partial_failure"),await db.list_tasks(job)
            assert await db.get_output(comedy,lib["id"]) is None and not original_path.exists()
            assert Path((await db.get_output(drama,lib["id"]))["strm_path"]).exists()
            assert not (await db.get_library(lib["id"]))["operation_pending"]
        finally:
            await engine.stop()
    asyncio.run(run())


def test_library_scope_change_stops_before_saving(make_store,boot):
    async def run():
        db,store = await make_store()
        await seed(db,store,"abc-001")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            preview = await prepare(engine,{"kind":"library","payload":{"values":{"name":"新库","dir":"new","rule":{"categories":["drama"]}}}})
            await seed(db,store,"abc-002")
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            result = await wait_job(db,job)
            assert result["state"]["partial_failure"]
            assert all(lib["name"]!="新库" for lib in await db.list_libraries())
            assert "重新预览" in (await db.list_tasks(job))[0]["last_error"]
        finally:
            await engine.stop()
    asyncio.run(run())


def test_new_rule_library_maps_preview_root_id(make_store,boot):
    async def run():
        db,store = await make_store()
        vid = await seed(db,store,"abc-001")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            preview = await prepare(engine,{"kind":"library","payload":{"values":{"name":"新库","dir":"new","rule":{"categories":["drama"]}}}})
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            result = await wait_job(db,job)
            assert not result["state"].get("partial_failure"),await db.list_tasks(job)
            assert await db.get_output(vid,result["state"]["library_id"])
        finally:
            await engine.stop()
    asyncio.run(run())


def test_large_explicit_selection_exceeds_old_page_limit(make_store,boot):
    async def run():
        db,store = await make_store()
        await store.update({"download_cover":False,"quality_capture":False})
        await db._write_many("INSERT INTO videos(slug,created_at,updated_at) VALUES(?,1,1)",[(f"synthetic-{i:04}",) for i in range(620)])
        ids = [r["id"] for r in await db._all("SELECT id FROM videos")]
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            lid = (await engine.create_library("目标","target"))["id"]
            preview = await prepare(engine,{"kind":"selection","payload":{"action":"add","library_id":lid,"ids":ids}})
            assert preview["state"]["_preview"]["counts"]["matched"]==620
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            result = await wait_job(db,job,timeout=30)
            assert not result["state"].get("partial_failure"),await db.task_counts(job)
            assert (await db._one("SELECT COUNT(*) AS n FROM outputs WHERE library_id=?",(lid,)))["n"]==620
        finally:
            await engine.stop()
    asyncio.run(run())


def test_output_sync_survives_restart_failure_and_new_revision(make_store,boot):
    async def run():
        db,store = await make_store()
        engine = build(db,store,boot,FakeFetcher("",{}))
        await store.update({"public_base_url":"http://new.invalid"})
        first = await output_sync.view(db)
        assert first["status"]=="pending"
        job = await engine.create_rewrite()
        await db.update_job(job,status="done",state={"partial_failure":True})
        assert (await output_sync.view(db))["status"]=="partial_failure"
        await store.update({"play_token":"new-token"})
        second = await output_sync.view(db)
        assert second["revision"]!=first["revision"] and second["job_id"] is None
        await output_sync.finish(db,await db.get_job(job))
        assert (await output_sync.view(db))["revision"]==second["revision"]
        await store.load()
        assert (await output_sync.view(db))["status"]=="pending"
    asyncio.run(run())


def test_partial_library_change_blocks_auto_rules_and_resumes(make_store,boot,monkeypatch):
    async def run():
        db,store = await make_store()
        await store.update({"max_attempts":1,"download_cover":False,"quality_capture":False})
        first = await seed(db,store,"abc-001")
        second = await seed(db,store,"abc-002")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            preview = await prepare(engine,{"kind":"library","payload":{"values":{"name":"新库","dir":"new","rule":{"categories":["drama"]}}}})
            update = db.update_job
            interrupted = []
            async def fail_once(job_id,**fields):
                if fields.get("state",{}).get("processed")==1 and not interrupted:
                    interrupted.append(job_id)
                    raise RuntimeError("simulated interruption after file change")
                return await update(job_id,**fields)
            monkeypatch.setattr(db,"update_job",fail_once)
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            failed = await wait_job(db,job)
            assert failed["state"]["partial_failure"]
            lid = failed["state"]["library_id"]
            assert (await db.get_library(lid))["operation_pending"]
            assert await db.get_output(first,lid) and await db.get_output(second,lid) is None
            await engine.apply_rules(await db.get_video_by_id(second),only_library=lid)
            assert await db.get_output(second,lid) is None
            monkeypatch.setattr(db,"update_job",update)
            await engine.set_job_status(job,"retry")
            done = await wait_job(db,job)
            assert not done["state"].get("partial_failure"),await db.list_tasks(job)
            assert await db.get_output(second,lid) and not (await db.get_library(lid))["operation_pending"]
        finally:
            await engine.stop()
    asyncio.run(run())


def test_retirement_preview_detects_changes_then_archives(make_store,boot):
    async def run():
        db,store = await make_store()
        await store.update({"download_cover":False,"quality_capture":False})
        vid = await seed(db,store,"abc-001")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            await db.ensure_output(vid,1)
            await engine._output_one(await db.get_video_by_id(vid),1,cover=False)
            before = Path((await db.get_output(vid,1))["strm_path"])
            stale = await prepare(engine,{"kind":"retirement","payload":{"site":"jable"}})
            await seed(db,store,"abc-002")
            bad = await engine.operations.approve(stale["id"],stale["state"]["preview_token"])
            assert (await wait_job(db,bad))["state"]["partial_failure"]
            assert store.current.site("jable").enabled and before.exists()
            preview = await prepare(engine,{"kind":"retirement","payload":{"site":"jable"}})
            apply = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            parent = await wait_job(db,apply)
            assert not parent["state"].get("partial_failure"),await db.list_tasks(apply)
            child = await wait_job(db,parent["state"]["child_job_id"])
            assert not child["state"].get("partial_failure"),await db.list_tasks(child["id"])
            assert not before.exists() and await db.get_output(vid,1) is None
            assert not store.current.site("jable").enabled
            assert await db.get_archive(vid,1)
        finally:
            await engine.stop()
    asyncio.run(run())


def test_job_api_pagination_lookup_target_search_and_idempotency(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.engine.pause()
        async def setup():
            for i in range(120):
                await ctx.db.create_job("videos",f"历史任务 {i}",{},status="done")
            await ctx.db.add_tasks(1,"detail",[f"abc-{i:04}" for i in range(350)])
        client.portal.call(setup)
        page = client.get("/api/jobs?paged=true&limit=50&offset=100").json()
        assert page["total"]==120 and len(page["items"])==20
        assert client.get("/api/jobs/1").json()["id"]==1
        tasks = client.get("/api/jobs/1/tasks?paged=true&q=abc-0349").json()
        assert tasks["total"]==1 and tasks["items"][0]["target"]=="abc-0349"
        header = {"Idempotency-Key":"same-create-request"}
        first = client.post("/api/jobs",json={"kind":"backfill"},headers=header)
        second = client.post("/api/jobs",json={"kind":"backfill"},headers=header)
        assert first.status_code==second.status_code==200 and first.json()==second.json()
        different = client.post("/api/jobs",json={"kind":"rewrite"},headers=header)
        assert different.status_code==409


def test_legacy_mutation_apis_require_matching_preview(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        body = {"name":"规则库","dir":"rules","rule":{"categories":["drama"]}}
        assert client.post("/api/libraries",json=body).status_code==409
        assert client.post("/api/sites/jable/retirement").status_code==409
        async def setup():
            await ctx.store.update({"download_cover":False,"quality_capture":False})
            await seed(ctx.db,ctx.store,"abc-001")
            return await prepare(ctx.engine,{"kind":"library","payload":{"values":body}})
        preview = client.portal.call(setup)
        receipt = client.get(f"/api/jobs/{preview['id']}").json()
        assert all(not key.startswith("_") for key in receipt["state"])
        confirmation = {**body,"preview_id":preview["id"],"preview_token":preview["state"]["preview_token"]}
        assert client.post("/api/libraries",json={**confirmation,"dir":"other"}).status_code==409
        response = client.post("/api/libraries",json=confirmation)
        assert response.status_code==200,response.text
        job_id = response.json()["job_id"]
        client.portal.call(wait_job,ctx.db,job_id)
        assert client.post("/api/libraries",json=confirmation).json()==response.json()
        retirement = client.portal.call(prepare,ctx.engine,{"kind":"retirement","payload":{"site":"jable"}})
        token = {"preview_id":retirement["id"],"preview_token":retirement["state"]["preview_token"]}
        assert client.post("/api/sites/missav/retirement",json=token).status_code==409
        first = client.post("/api/sites/jable/retirement",json=token)
        assert first.status_code==202,first.text
        assert client.post("/api/sites/jable/retirement",json=token).json()==first.json()


def test_file_changes_drains_blocks_writers_and_recovers_cancellation():
    async def run():
        changes = FileChanges()
        entered = [asyncio.Event(),asyncio.Event()]
        release = asyncio.Event()
        async def normal(index):
            async with changes.normal():
                async with changes.normal():
                    entered[index].set()
                    await release.wait()
        writers = [asyncio.create_task(normal(i)) for i in range(2)]
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)),1)
        exclusive_entered = asyncio.Event()
        exclusive_release = asyncio.Event()
        async def exclusive():
            async with changes.exclusive():
                exclusive_entered.set()
                async with changes.normal():
                    await exclusive_release.wait()
        operation = asyncio.create_task(exclusive())
        await asyncio.sleep(0)
        assert not changes.ready.is_set() and not exclusive_entered.is_set()
        late_entered = asyncio.Event()
        async def late():
            async with changes.normal():
                late_entered.set()
        blocked = asyncio.create_task(late())
        await asyncio.sleep(0)
        assert not late_entered.is_set()
        release.set()
        await asyncio.gather(*writers)
        await asyncio.wait_for(exclusive_entered.wait(),1)
        assert not late_entered.is_set()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await asyncio.wait_for(blocked,1)
        assert changes.ready.is_set() and changes.drained.is_set()
    asyncio.run(run())


def test_selection_enqueue_cursor_is_atomic_and_resumable(make_store,boot,monkeypatch):
    async def run():
        db,store = await make_store()
        await db._write_many("INSERT INTO videos(slug,created_at,updated_at) VALUES(?,1,1)",[(f"selected-{i:04}",) for i in range(420)])
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.reload_libraries()
        preview_id = await engine.operations.create({"kind":"selection","payload":{"action":"add","library_id":1,"query":{}}})
        await engine.operations.preview(await db.get_job(preview_id),{})
        await db.update_job(preview_id,status="done")
        preview = await db.get_job(preview_id)
        job_id = await engine.operations.approve(preview_id,preview["state"]["preview_token"])
        checkpoint = engine.checkpoint
        calls = 0
        async def stop_after_batch(job):
            nonlocal calls
            calls += 1
            if calls==2:
                raise RuntimeError("interrupted between batches")
            await checkpoint(job)
        monkeypatch.setattr(engine,"checkpoint",stop_after_batch)
        with pytest.raises(RuntimeError):
            await engine.operations.selected(await db.get_job(job_id),{})
        interrupted = await db.get_job(job_id)
        assert interrupted["state"]["cursor"]==200
        assert (await db._one("SELECT COUNT(*) AS n FROM tasks WHERE job_id=? AND kind='membership'",(job_id,)))["n"]==200
        monkeypatch.setattr(engine,"checkpoint",checkpoint)
        await engine.operations.selected(await db.get_job(job_id),{})
        assert (await db._one("SELECT COUNT(*) AS n FROM tasks WHERE job_id=? AND kind='membership'",(job_id,)))["n"]==420
    asyncio.run(run())


def test_retirement_setup_retries_same_child_after_settings_failure(make_store,boot,monkeypatch):
    async def run():
        db,store = await make_store()
        await store.update({"max_attempts":1,"download_cover":False,"quality_capture":False})
        await seed(db,store,"abc-001")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            preview = await prepare(engine,{"kind":"retirement","payload":{"site":"jable"}})
            update = store.update
            async def unavailable(patch):
                raise RuntimeError("settings write interrupted")
            monkeypatch.setattr(store,"update",unavailable)
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            failed = await wait_job(db,job)
            assert failed["state"]["partial_failure"]
            child = failed["state"]["child_job_id"]
            assert (await db.get_job(child))["status"]=="paused" and store.current.site("jable").enabled
            monkeypatch.setattr(store,"update",update)
            await engine.set_job_status(job,"retry")
            done = await wait_job(db,job)
            assert not done["state"].get("partial_failure"),await db.list_tasks(job)
            assert done["state"]["child_job_id"]==child
            assert not (await wait_job(db,child))["state"].get("partial_failure")
            assert (await db._one("SELECT COUNT(*) AS n FROM jobs WHERE kind='source_remove'"))["n"]==1
        finally:
            await engine.stop()
    asyncio.run(run())


def test_library_preview_ignores_url_refresh_and_preserves_external_artifacts(make_store,boot,tmp_path):
    async def run():
        db,store = await make_store()
        await store.update({"download_cover":False,"quality_capture":False})
        vid = await seed(db,store,"abc-001")
        engine = build(db,store,boot,FakeFetcher("",{}))
        await engine.start()
        try:
            lib = await engine.create_library("外部库","inbox",external_dir=str(tmp_path/"external"))
            await engine.add_to_library([vid],lib["id"])
            original = Path((await db.get_output(vid,lib["id"]))["strm_path"])
            moved = tmp_path/"external"/"organized"/"renamed.strm"
            moved.parent.mkdir(parents=True)
            original.rename(moved)
            nfo,poster = moved.with_suffix(".nfo"),moved.with_suffix(".jpg")
            nfo.write_bytes(b"external nfo")
            poster.write_bytes(b"external picture")
            await engine.strm.locate(engine._library(lib["id"]))
            old = await db.get_library(lib["id"])
            values = {k:old[k] for k in ("name","dir","external_dir","path_template","versions","rule","sources","excludes")}
            values["versions"] = "emby"
            preview = await prepare(engine,{"kind":"library","payload":{"library_id":lib["id"],"values":values}})
            await db._write("UPDATE videos SET detail_at=detail_at+100 WHERE id=?",(vid,))
            await db._write("UPDATE sources SET stream_url='https://new.invalid/signed-url' WHERE video_id=?",(vid,))
            job = await engine.operations.approve(preview["id"],preview["state"]["preview_token"])
            done = await wait_job(db,job)
            assert not done["state"].get("partial_failure"),await db.list_tasks(job)
            assert moved.exists() and not original.exists()
            assert nfo.read_bytes()==b"external nfo" and poster.read_bytes()==b"external picture"
        finally:
            await engine.stop()
    asyncio.run(run())


def test_concurrent_settings_updates_preserve_both_patches(make_store):
    async def run():
        db,store = await make_store()
        await asyncio.gather(store.update({"play_token":"new-token"}),store.update({"public_base_url":"https://new.invalid"}))
        await store.load()
        assert store.current.play_token=="new-token" and store.current.public_base_url=="https://new.invalid"
        assert set((await output_sync.view(db))["keys"])=={"play_token","public_base_url"}
    asyncio.run(run())
