import asyncio
import json
from pathlib import Path

import pytest

from hls2strm.operations import ScopeChanged
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher
from .test_engine import build


async def setup(make_store, boot):
    db, store = await make_store()
    await store.update({"quality_capture": False, "download_cover": False})
    e = build(db, store, boot, FakeFetcher("", {}))
    await e.reload_libraries()
    return db, store, e


async def pair(db, store, offset=0):
    ids = []
    for n, slug in enumerate((f"259luxu-{1850+offset}-u", f"luxu-{1850+offset}-u")):
        vid = await db.upsert_detail("jable", SourceDetail(key=slug, code=f"TEMP-{offset*2+n+1}", title=slug,
                                     stream_url="https://cdn.invalid/movie.m3u8"), slug, store.current.site_rank)
        ids.append(vid)
    for vid, code in zip(ids, (f"259LUXU-{1850+offset}", f"LUXU-{1850+offset}")):
        await db._write("UPDATE videos SET code=?,code_key=?,uncensored=1 WHERE id=?", (code, f"LUXU-{1850+offset}", vid))
    return ids


async def preview(e, payload=None):
    pid = await e.operations.create({"kind": "dedupe", "payload": payload or {}})
    await e.operations.preview(await e.db.get_job(pid), {})
    await e.db.update_job(pid, status="done")
    return await e.db.get_job(pid)


async def approve(e, p):
    jid = await e.operations.approve(p["id"], p["state"]["preview_token"])
    return await e.db.get_job(jid)


async def output(e, vid, lid, path):
    await e.db.ensure_output(vid, lid)
    v = await e.db.get_video_by_id(vid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(e.writer.play_url(v) + "\n", encoding="utf-8")
    await e.db.set_output(vid, lid, str(path))
    return v


@pytest.mark.parametrize("location", ["exclusive", "inbox", "shared"])
def test_cleanup_and_versions_use_existing_paths(make_store, boot, tmp_path, monkeypatch, location):
    async def run():
        db, store, e = await setup(make_store, boot)
        loser, keep = await pair(db, store)
        ext, inbox = tmp_path / "organized", store.output_dir / "inbox"
        lib = await e.create_library("外部", str(inbox), external_dir=str(ext), versions="suffix")
        keeper_path = ext / "KEEP" / "KEEP.strm"
        donor_path = (inbox if location == "inbox" else ext) / "DONOR" / "DONOR.strm"
        keeper = await output(e, keep, lib["id"], keeper_path)
        donor = await output(e, loser, lib["id"], donor_path)
        keeper_path.with_suffix(".nfo").write_text("keep-nfo", encoding="utf-8")
        donor_path.with_suffix(".nfo").write_text("donor-nfo", encoding="utf-8")
        if location == "shared":
            (donor_path.parent / "OTHER.strm").write_text("https://other.invalid/movie", encoding="utf-8")
        e.writer.sync_versions(donor, donor_path, ["1080p", "720p"], "suffix")
        # 类似文件名、其他 slug 的附件必须保留。
        foreign = donor_path.with_name("DONOR-480p.strm")
        if location == "shared":
            foreign.write_text("http://h/play/other@480p.m3u8", encoding="utf-8")
        await db._write("UPDATE sources SET heights='1080,720',height=1080 WHERE video_id=?", (loser,))
        await db._write("UPDATE sources SET heights='720,480',height=720 WHERE video_id=?", (keep,))
        # 两边都在整理目录时按播放次数保留第二部。
        await db._write("INSERT INTO play_history(session_key,created_at,updated_at,slug,video_id,entry,state,http_status) VALUES('x',1,1,?,?,'play','delivered',200)", (keeper["slug"], keep))
        p = await preview(e)
        row = json.loads((await db._one("SELECT data FROM operation_items WHERE preview_id=?", (p["id"],)))["data"])
        assert row["keep"] == keep and row["selected"]
        assert p["state"]["_preview"]["counts"]["strm_files"] == 3
        assert len(row["retire_dirs"]) == (location == "exclusive")
        remove = e._remove_files
        async def checked(*args):
            assert e.file_changes.owner is asyncio.current_task()
            assert (await db.get_video(donor["slug"]))["id"] == loser
            assert not await db._one("SELECT 1 FROM video_aliases WHERE slug=?", (donor["slug"],))
            await remove(*args)
        monkeypatch.setattr(e, "_remove_files", checked)
        await e.operations.apply(await approve(e, p), {})
        assert not donor_path.exists() and not donor_path.with_name("DONOR-1080p.strm").exists()
        assert keeper_path.exists() and keeper_path.with_suffix(".nfo").read_text() == "keep-nfo"
        assert keeper_path.with_name("KEEP-1080p.strm").exists()
        assert "@1080p" in keeper_path.with_name("KEEP-1080p.strm").read_text()
        if location == "exclusive":
            assert list((ext / ".hls2strm-trash").rglob("DONOR.nfo"))
        else:
            assert donor_path.with_suffix(".nfo").exists()
        if location == "shared":
            assert foreign.exists()
        assert (await db.get_video(donor["slug"]))["id"] == keep
        assert await db.get_video_by_id(loser) is None
        assert (loser, donor["slug"]) not in await db.video_keys()
        assert (keep, donor["slug"]) in await db.video_keys()
    asyncio.run(run())


def test_data_merge_removed_checks_archives_and_aliases(make_store, boot, tmp_path):
    async def run():
        db, store, e = await setup(make_store, boot)
        loser, keep = await pair(db, store)
        await db._write("UPDATE sources SET status='removed' WHERE video_id=?", (loser,))
        await db._write("UPDATE videos SET maker='maker',director='director' WHERE id=?", (loser,))
        await db.set_source_check(keep, "missav", False)
        await db._write("UPDATE source_checks SET checked_at=200 WHERE video_id=?", (keep,))
        await db.set_source_check(loser, "missav", True)
        await db._write("UPDATE source_checks SET checked_at=100 WHERE video_id=?", (loser,))
        await db._write("INSERT INTO video_aliases VALUES('older-u',?,1,NULL)", (loser,))
        await db._write("INSERT INTO crawl_items VALUES(99,?,1,'jable','a',1,'old-slug','old-title',1,1,0)", (loser,))
        await db._write("INSERT INTO crawl_items VALUES(100,?,1,'jable','a',1,'old-slug','old-title',1,1,0)", (loser,))
        await db._write("INSERT INTO crawl_items VALUES(100,?,2,'jable','b',2,'keep-slug','keep-title',0,0,1)", (keep,))
        await db._write("INSERT INTO play_history(session_key,created_at,updated_at,slug,video_id,entry,state,http_status) VALUES('x',1,1,'old-slug',?,'play','delivered',200)", (loser,))
        archived = tmp_path / "archived.strm"
        archived.write_text("keep archived", encoding="utf-8")
        await db.add_archive(loser, 1, "old.strm", [("old.strm", str(archived))])
        await db.ensure_output(loser, 1, "source")
        p = await preview(e, {"keep": {str(loser): keep}})
        await e.operations.apply(await approve(e, p), {})
        v = await db.get_video_by_id(keep)
        assert v["maker"] == "maker" and v["director"] == "director"
        assert {s["status"] for s in await db.get_sources(keep)} == {"active", "removed"}
        check = await db.get_source_check(keep, "missav")
        assert (check["found"], check["checked_at"]) == (1, 200)
        assert (await db.get_video("older-u"))["id"] == keep
        history = await db._one("SELECT video_id,slug FROM play_history")
        assert tuple(history) == (keep, "old-slug")
        crawl = await db._one("SELECT video_id,slug,title FROM crawl_items WHERE job_id=99")
        assert tuple(crawl) == (keep, "old-slug", "old-title")
        clash = await db._one("SELECT video_id,slug,title,is_new,added,excluded FROM crawl_items WHERE job_id=100")
        assert tuple(clash) == (keep, "keep-slug", "keep-title", 1, 1, 0)
        assert archived.read_text() == "keep archived" and await db.get_archive(loser, 1) is None
    asyncio.run(run())


def test_preview_options_conflicts_and_native_correction(make_store, boot):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        await db._write("UPDATE videos SET release_date=CASE id WHEN ? THEN '2020-01-01' ELSE '2021-01-01' END", (a,))
        p = await preview(e)
        assert p["state"]["_preview"]["counts"]["selected_groups"] == 0
        await e.operations.apply(await approve(e, p), {})
        assert await db.get_video_by_id(a) and await db.get_video_by_id(b)
        p = await preview(e, {"allow_conflicts": [a], "skip": [a]})
        assert p["state"]["_preview"]["counts"]["selected_groups"] == 0
        p = await preview(e, {"allow_conflicts": [a], "keep": {str(a): b}})
        await e.operations.apply(await approve(e, p), {})
        assert await db.get_video_by_id(a) is None
        await db._write("UPDATE videos SET code='PONDO-100826_001',code_key='1PON-100826_001',uncensored=1 WHERE id=?", (b,))
        p = await preview(e)
        assert p["state"]["_preview"]["counts"]["edition_corrections"] == 1
        await e.operations.apply(await approve(e, p), {})
        assert not (await db.get_video_by_id(b))["uncensored"]
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["cleanup", "merged"])
def test_resume_after_file_or_database_side_effect(make_store, boot, tmp_path, monkeypatch, stage):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        path = store.output_dir / "donor" / "donor.strm"
        await output(e, a, 1, path)
        p = await preview(e, {"keep": {str(a): b}})
        job = await approve(e, p)
        original = e._remove_files if stage == "cleanup" else e._output_all
        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("模拟中断")
        name = "_remove_files" if stage == "cleanup" else "_output_all"
        monkeypatch.setattr(e, name, interrupted)
        with pytest.raises(RuntimeError, match="模拟中断"):
            await e.operations.apply(job, {})
        assert not path.exists()
        assert (await db.get_video_by_id(a) is None) == (stage == "merged")
        monkeypatch.setattr(e, name, original)
        await e.operations.apply(await db.get_job(job["id"]), {})
        assert await db.get_video_by_id(a) is None
        assert (await db.get_video("259luxu-1850-u"))["id"] == b
        await e.operations.apply(await db.get_job(job["id"]), {})
        assert len(await db.get_sources(b)) == 2
    asyncio.run(run())


def test_scope_change_and_external_move(make_store, boot, tmp_path):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        ext = tmp_path / "external"
        lib = await e.create_library("外部", "inbox", external_dir=str(ext))
        original = ext / "original" / "donor.strm"
        await output(e, a, lib["id"], original)
        p = await preview(e, {"keep": {str(a): b}})
        await db._write("UPDATE videos SET code_key='CHANGED-1' WHERE id=?", (a,))
        job = await approve(e, p)
        await e.operations.apply(job, {})
        state = (await db.get_job(job["id"]))["state"]
        assert state["completed"] and state["skipped"] == 1
        assert state["skipped_groups"][0]["group_id"] == a
        assert original.exists()
        await db._write("UPDATE videos SET code_key='LUXU-1850' WHERE id=?", (a,))
        p = await preview(e, {"keep": {str(a): b}})
        moved = ext / "moved" / "renamed.strm"
        moved.parent.mkdir(parents=True)
        original.rename(moved)
        await e.operations.apply(await approve(e, p), {})
        assert not moved.exists()
    asyncio.run(run())


def test_current_slug_preferred_over_residual_alias(make_store, boot, tmp_path):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        p = await preview(e, {"keep": {str(a): b}})
        await e.operations.apply(await approve(e, p), {})
        ext = tmp_path / "external"
        lib = await e.create_library("外部", "inbox", external_dir=str(ext))
        old = ext / "AAA" / "old.strm"
        old.parent.mkdir(parents=True)
        old.write_text("http://h/play/259luxu-1850-u.m3u8", encoding="utf-8")
        current = ext / "ZZZ" / "current.strm"
        await output(e, b, lib["id"], current)
        await db.set_output(b, lib["id"], str(old))
        lib = e.libs[lib["id"]]
        paths = (await e.strm.index(lib, max_age=0))[b]
        assert paths == [str(current), str(old)] and e.strm.pick(lib, paths) == str(current)
        await e.strm.locate(lib)
        assert (await db.get_output(b, lib["id"]))["strm_path"] == str(current)
    asyncio.run(run())


def test_merge_uses_normal_exclusion_settle(make_store, boot, tmp_path):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        high = (await e.create_library("高优先", "high"))["id"]
        low = (await e.create_library("低优先", "low", excludes=[high]))["id"]
        old_high = store.output_dir / "high" / "old.strm"
        old_low = store.output_dir / "low" / "old.strm"
        await output(e, a, high, old_high)
        await output(e, b, low, old_low)
        p = await preview(e, {"keep": {str(a): b}})
        assert p["state"]["_preview"]["counts"]["library_changes"] == 1
        assert p["state"]["_preview"]["attention"][0]["predicted_libraries"] == [high]
        await e.operations.apply(await approve(e, p), {})
        assert e._settle_wanted
        await e.settle()
        assert not await db.get_output(b, low)
        assert Path((await db.get_output(b, high))["strm_path"]).exists()
        assert not old_low.exists() and not old_high.exists()
    asyncio.run(run())


def test_completed_groups_survive_later_scope_change(make_store, boot, monkeypatch):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        c, d = await pair(db, store, 1)
        f, g = await pair(db, store, 2)
        p = await preview(e)
        job = await approve(e, p)
        original = e._output_all
        async def changed(v, **kwargs):
            await original(v, **kwargs)
            await db._write("UPDATE videos SET status='gone' WHERE id=?", (c,))
        monkeypatch.setattr(e, "_output_all", changed)
        await e.operations.apply(job, {})
        assert await db.get_video_by_id(a) and not await db.get_video_by_id(b)
        assert await db.get_video_by_id(c) and await db.get_video_by_id(d)
        assert await db.get_video_by_id(f) and not await db.get_video_by_id(g)
        state = (await db.get_job(job["id"]))["state"]
        assert state["processed"] == 2 and state["skipped"] == 1 and state["completed"]
    asyncio.run(run())


@pytest.mark.parametrize("when", ["before", "after_cleanup"])
def test_metadata_refresh_does_not_stop_merge(make_store, boot, monkeypatch, when):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        path = store.output_dir / "old" / "old.strm"
        await output(e, a, 1, path)
        p = await preview(e, {"keep": {str(a): b}})
        async def refresh():
            await db._write("UPDATE videos SET title='新标题',duration=900,detail_at=99,maker='新片商',release_date='2020-01-01',tags='[]' WHERE id=?", (a,))
            await db._write("UPDATE videos SET title='保留者新标题',detail_at=101 WHERE id=?", (b,))
        if when == "before":
            await refresh()
        else:
            remove = e._remove_files
            async def changed(*args):
                await remove(*args)
                assert not path.exists()
                await refresh()
            monkeypatch.setattr(e, "_remove_files", changed)
        job = await approve(e, p)
        await e.operations.apply(job, {})
        assert await db.get_video_by_id(a) is None
        v = await db.get_video_by_id(b)
        assert v["title"] == "保留者新标题" and v["duration"] == 900 and v["maker"] == "新片商"
        assert v["detail_at"] == 101 and not path.exists()
        state = (await db.get_job(job["id"]))["state"]
        assert state["processed"] == 1 and not state.get("skipped_groups")
    asyncio.run(run())


@pytest.mark.parametrize("change", ["code_key", "uncensored", "status", "source", "output", "deleted"])
def test_changed_group_is_skipped_before_any_file_removal(make_store, boot, monkeypatch, caplog, change):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        c, d = await pair(db, store, 1)
        path = store.output_dir / "old" / "old.strm"
        await output(e, a, 1, path)
        p = await preview(e, {"keep": {str(a): b}})
        if change == "source":
            await db._write("UPDATE sources SET status='removed' WHERE video_id=?", (a,))
        elif change == "output":
            await db._write("UPDATE outputs SET via='rule' WHERE video_id=?", (a,))
        elif change == "deleted":
            await db._write("DELETE FROM videos WHERE id=?", (a,))
        else:
            value = {"code_key": "OTHER-1", "uncensored": 0, "status": "gone"}[change]
            await db._write(f"UPDATE videos SET {change}=? WHERE id=?", (value, a))
        remove = e._remove_files
        async def checked(lib, vid, *args):
            assert vid != a
            await remove(lib, vid, *args)
        monkeypatch.setattr(e, "_remove_files", checked)
        job = await approve(e, p)
        await e.operations.apply(job, {})
        state = (await db.get_job(job["id"]))["state"]
        assert state["skipped"] == 1 and state["processed"] == 1 and state["completed"]
        assert state["skipped_groups"][0]["group_id"] == a
        assert path.exists() and await db.get_video_by_id(b)
        assert await db.get_video_by_id(c) and not await db.get_video_by_id(d)
        assert any(f"跳过组 #{a}" in record.getMessage() for record in caplog.records)
        await e.operations.apply(await db.get_job(job["id"]), {})
        assert (await db.get_job(job["id"]))["state"]["skipped"] == 1
    asyncio.run(run())


def test_confirmed_preview_fingerprint_still_checked(make_store, boot):
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        p = await preview(e)
        row = await db._one("SELECT data FROM operation_items WHERE preview_id=?", (p["id"],))
        item = json.loads(row["data"])
        item["keep"] = b
        await db._write("UPDATE operation_items SET data=? WHERE preview_id=?", (json.dumps(item), p["id"]))
        with pytest.raises(ScopeChanged, match="预览记录"):
            await e.operations.apply(await approve(e, p), {})
        assert await db.get_video_by_id(a) and await db.get_video_by_id(b)
    asyncio.run(run())


@pytest.mark.parametrize("old,expected", [(0, 42), (7, 7)])
def test_merge_and_prediction_share_numeric_empty_values(make_store, boot, monkeypatch, old, expected):
    from hls2strm import dedupe

    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        fields = ("duration", "views", "likes", "favs", "detail_at")
        sets = ",".join(f"{key}=?" for key in fields)
        await db._write(f"UPDATE videos SET {sets} WHERE id=?", (*([42]*len(fields)), a))
        await db._write(f"UPDATE videos SET {sets},uncensored=0 WHERE id=?", (*([old]*len(fields)), b))
        # 原生无码的原始版本标记 0 不能跟数值元数据一起当成空。
        await db._write("UPDATE videos SET code='1PON-010120_001',code_key='1PON-010120_001'")
        donor, keeper = await db.get_video_by_id(a), await db.get_video_by_id(b)
        donor["outputs"], keeper["outputs"] = [], []
        seen = []
        def inspect(rule, v):
            seen.append(dict(v))
            return True
        monkeypatch.setattr(dedupe, "match_rule", inspect)
        libs = {1: {"id": 1, "rule": {"keywords": ["test"]}, "sources": [], "excludes": []}}
        assert dedupe.predict(libs, [donor, keeper], keeper) == [1]
        assert all(seen[0][key] == expected for key in fields)
        assert seen[0]["uncensored"] == 0
        p = await preview(e, {"keep": {str(a): b}})
        await e.operations.apply(await approve(e, p), {})
        result = await db.get_video_by_id(b)
        assert all(result[key] == expected for key in fields)
    asyncio.run(run())


def test_old_slug_api_and_play_keep_quality(tmp_path):
    from fastapi.testclient import TestClient
    from hls2strm.app import create_app
    from hls2strm.config import BootConfig
    from hls2strm.play import Resolved
    from hls2strm.sites import SITES

    app = create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.engine.pause()
        async def seed():
            await ctx.store.update({"quality_capture": False, "download_cover": False})
            vid = await ctx.db.upsert_detail("jable", SourceDetail(key="luxu-1-u", code="LUXU-1", title="t", uncensored=True,
                                            stream_url="https://cdn.invalid/main.m3u8"), "luxu-1-u", ctx.store.current.site_rank)
            await ctx.db._write("INSERT INTO video_aliases VALUES('old-u',?,1,NULL)", (vid,))
            return vid
        vid = client.portal.call(seed)
        seen = []
        async def choose(slug, **kw):
            v = await ctx.db.get_video(slug)
            seen.append((v["id"], kw.get("want")))
            return Resolved(v, (await ctx.db.get_sources(v["id"]))[0], SITES["jable"])
        ctx.resolver.selection.choose = choose
        assert client.get("/api/videos/old-u").json()["id"] == vid
        assert [v["id"] for v in client.get("/api/videos?q=old-u").json()["items"]] == [vid]
        result = client.get("/play/old-u@720p.m3u8?prepare=1")
        assert result.status_code == 200 and seen == [(vid, 720)]


@pytest.mark.parametrize("change", ["source", "status", "key"])
def test_changes_after_cleanup_do_not_strand_group(make_store, boot, monkeypatch, change):
    """开始清文件以后，源、状态的变化不影响合并；只有番号/版本变了才跳过，并说明文件已清理。"""
    async def run():
        db, store, e = await setup(make_store, boot)
        a, b = await pair(db, store)
        path = store.output_dir / "donor" / "donor.strm"
        await output(e, a, 1, path)
        p = await preview(e, {"keep": {str(a): b}})
        job = await approve(e, p)
        remove = e._remove_files

        async def concurrent(lib, vid, *args):
            await remove(lib, vid, *args)
            if change == "source":  # 订阅这时给保留者挂上新源
                await db.upsert_detail("missav", SourceDetail(key="luxu-1850-uncensored-leak", code="LUXU-1850",
                                       title="x", uncensored=True), "luxu-1850-u", store.current.site_rank, video_id=b)
            elif change == "status":
                await db._write("UPDATE videos SET status='gone' WHERE id=?", (b,))
            else:
                await db._write("UPDATE videos SET code_key='OTHER-1' WHERE id=?", (b,))
        monkeypatch.setattr(e, "_remove_files", concurrent)
        await e.operations.apply(job, {})
        state = (await db.get_job(job["id"]))["state"]
        assert not path.exists()
        if change == "key":
            assert state["skipped"] == 1 and await db.get_video_by_id(a)
            assert "文件已清理" in state["skipped_groups"][0]["reason"]
            assert state["skipped_groups"][0]["code_key"] == "LUXU-1850"
        else:
            assert state["processed"] == 1 and not state.get("skipped")
            assert await db.get_video_by_id(a) is None
            assert {s["site"] for s in await db.get_sources(b)} == ({"jable", "missav"} if change == "source" else {"jable"})
    asyncio.run(run())
