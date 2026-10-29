import asyncio
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import pytest

from hls2strm import metadata_files
from hls2strm.metadata_fallback import RepairRequest
from hls2strm.metadata_patch import patch_nfo
from hls2strm.engine import TaskStopped
from hls2strm.nfo import parse
from .test_maintenance import fixture


async def seed(make_store, boot, tmp_path):
    e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
    await db._write("UPDATE videos SET title=?,maker=?,models=?,cover_url=?,duration=5400 WHERE id=?",
                    ("Source title", "Studio", json.dumps([{"name": "Actor"}]), "https://example.test/cover.jpg", vid))
    return e, db, lid, vid, directory


async def preview(e, lid, mode, **kwargs):
    if mode == "takeover":
        kwargs.setdefault("allow_empty_external", True)
    jid = await e.metadata.create(RepairRequest(library_id=lid, mode=mode, **kwargs))
    await e.metadata.preview(await e.db.get_job(jid), {})
    await e.db.update_job(jid, status="done")
    return await e.db.get_job(jid)


async def execute(e, p):
    jid = await e.metadata.approve(p["id"], p["state"]["preview_token"], True)
    await e.metadata.apply(await e.db.get_job(jid), {})
    await e.db.update_job(jid, status="done")
    return await e.db.get_job(jid)


def test_patch_preserves_extensions_and_never_invents_dates_or_plot():
    original = b'<movie><!--keep--><title>Local</title><studio>Local studio</studio><ratings><rating name="x"><value>9</value></rating></ratings><custom foo="bar">yes</custom><actor><name>Local actor</name><thumb>photo</thumb></actor><set><name>Local set</name></set></movie>'
    video = {"title": "Source", "code": "TEST-001", "release_date": "2025-01-01", "maker": "Source studio",
             "models": [{"name": "New actor"}], "series": "New set", "duration": 5400, "tags": [{"name": "Tag"}]}
    content, changes = patch_nfo(video, original)
    root = ET.fromstring(content)
    assert root.findtext("title") == "Local"
    assert root.findtext("studio") == "Local studio"
    assert root.findtext("actor/thumb") == "photo"
    assert root.findtext("ratings/rating/value") == "9"
    assert root.find("custom").get("foo") == "bar" and b"<!--keep-->" in content
    assert root.findtext("runtime") == "90" and root.findtext("tag") == "Tag"
    assert not root.findall("premiered") and not root.findall("plot")
    assert patch_nfo(video, content)[1] == []
    assert changes


@pytest.mark.parametrize("mode", ["missing", "fields"])
def test_modes_are_independent_and_idempotent(make_store, boot, tmp_path, mode):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        nfo = directory / "TEST-001.nfo"
        if mode == "missing":
            nfo.unlink()
        else:
            nfo.write_text('<movie><num>TEST-001</num><title>Local</title><custom>keep</custom></movie>', encoding="utf-8")
        p = await preview(e, lid, mode)
        assert p["state"]["eligible"] == 1, p
        before = nfo.read_bytes() if nfo.exists() else None
        assert not (directory / "TEST-001-fanart.jpg").exists()
        result = await execute(e, p)
        assert result["state"].get("done") == 1, result
        parsed = parse(nfo.read_bytes())
        assert parsed["models"] == ["Actor"]
        assert (directory / "poster.jpg").read_bytes() == b"poster"
        assert (directory / "TEST-001-fanart.jpg").exists() == (mode == "missing")
        if mode == "fields":
            assert parsed["title"] == "Local" and ET.fromstring(nfo.read_bytes()).findtext("custom") == "keep"
            item = await db._one("SELECT id FROM maintenance_items WHERE preview_id=?", (p["id"],))
            assert (e.metadata.directory / str(item["id"]) / "original.nfo").read_bytes() == before
        again = await preview(e, lid, mode)
        assert not again["state"].get("eligible")
        assert (await db.get_video_by_id(vid))["title"] == "Source title"
        await db.close()
    asyncio.run(run())


def test_missing_mode_does_not_patch_existing_nfo(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, directory = await seed(make_store, boot, tmp_path)
        nfo = directory / "TEST-001.nfo"
        old = nfo.read_bytes()
        p = await preview(e, lid, "missing")
        result = await execute(e, p)
        assert result["state"].get("done") == 1
        assert nfo.read_bytes() == old
        await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["file", "ban", "output", "invalid", "conflict"])
def test_changed_or_unsafe_entries_are_not_written(make_store, boot, tmp_path, change):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        nfo = directory / "TEST-001.nfo"
        if change == "invalid":
            nfo.write_text("broken", encoding="utf-8")
        if change == "conflict":
            nfo.write_text("<movie><num>OTHER-001</num></movie>", encoding="utf-8")
        p = await preview(e, lid, "missing")
        if change in ("invalid", "conflict"):
            assert not p["state"].get("eligible")
        else:
            if change == "file":
                nfo.write_text("<movie><title>Changed</title></movie>", encoding="utf-8")
            elif change == "ban":
                await db._write("INSERT INTO resource_exclusions VALUES(?,?,?,?)", (vid, lid, "fanart", p["id"]))
            else:
                await db.delete_output(vid, lid)
            result = await execute(e, p)
            assert result["state"].get("failed") == 1
        assert not (directory / "TEST-001-fanart.jpg").exists()
        await db.close()
    asyncio.run(run())


async def inbox_movie(e, db, lid, vid, directory):
    inbox = e.writer.library_root(e.libs[lid]) / "TEST-001"
    shutil.move(str(directory), str(inbox))
    await db.set_output(vid, lid, str(inbox / "TEST-001.strm"))
    return inbox


@pytest.mark.parametrize("interrupt", [False, True])
def test_takeover_preserves_sidecars_updates_path_and_resumes(make_store, boot, tmp_path, monkeypatch, interrupt):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        inbox = await inbox_movie(e, db, lid, vid, directory)
        p = await preview(e, lid, "takeover")
        assert p["state"].get("eligible") == 1, p
        old = metadata_files.publish
        count = 0

        def broken(path, prepared):
            nonlocal count
            old(path, prepared)
            count += 1
            if count == 2:
                raise OSError("simulated process interruption")

        if interrupt:
            monkeypatch.setattr(metadata_files, "publish", broken)
        result = await execute(e, p)
        target = e.writer.external_root(e.libs[lid]) / "_hls补全" / "test-001"
        if interrupt:
            assert result["state"].get("failed") == 1
            assert (inbox / "TEST-001.strm").exists()
            monkeypatch.setattr(metadata_files, "publish", old)
            await e.metadata.retry(result["id"])
            await e.metadata.apply(await db.get_job(result["id"]), {})
        assert (target / "TEST-001.strm").exists()
        assert (target / "poster.jpg").read_bytes() == b"poster"
        assert len(list((target / "extrafanart").glob("*.jpg"))) == 4
        assert not list(inbox.rglob("*.strm"))
        assert (await db.get_output(vid, lid))["strm_path"] == str(target / "TEST-001.strm")
        assert not list(e.metadata.directory.rglob("copy-*"))
        assert not list(e.metadata.directory.rglob("new-*"))
        assert not (await preview(e, lid, "takeover"))["state"].get("eligible")
        await db.close()
    asyncio.run(run())


def test_empty_external_requires_explicit_mount_confirmation(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        inbox = await inbox_movie(e, db, lid, vid, directory)
        p = await preview(e, lid, "takeover", allow_empty_external=False)
        assert not p["state"]["ready"] and "挂载" in p["state"]["error"]
        assert (inbox / "TEST-001.strm").exists()
        await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("policy", ["resources", "collection", "source"])
def test_existing_policy_is_respected(make_store, boot, tmp_path, policy):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        (directory / "TEST-001.nfo").unlink()
        if policy == "resources":
            for kind in ("nfo", "fanart", "poster"):
                await db._write("INSERT INTO resource_exclusions VALUES(?,?,?,1)", (vid, lid, kind))
        elif policy == "collection":
            await db._write("INSERT INTO collection_exclusions VALUES(?,?,1,'manual',0)", (vid, lid))
        else:
            await db._write("UPDATE sources SET status='removed' WHERE video_id=?", (vid,))
        p = await preview(e, lid, "missing")
        assert not p["state"].get("eligible")
        assert not (directory / "TEST-001.nfo").exists()
        await db.close()
    asyncio.run(run())


def test_takeover_retry_after_source_delete_before_db_update(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        inbox = await inbox_movie(e, db, lid, vid, directory)
        p = await preview(e, lid, "takeover", images=False)
        original = db.set_output_paths

        async def broken(*args):
            raise OSError("simulated interruption before output update")

        monkeypatch.setattr(db, "set_output_paths", broken)
        result = await execute(e, p)
        assert result["state"].get("failed") == 1
        assert not (inbox / "TEST-001.strm").exists()
        target = e.writer.external_root(e.libs[lid]) / "_hls补全" / "test-001"
        assert (target / "TEST-001.strm").exists()
        monkeypatch.setattr(db, "set_output_paths", original)
        await e.metadata.retry(result["id"])
        await e.metadata.apply(await db.get_job(result["id"]), {})
        assert (await db.get_job(result["id"]))["state"].get("done") == 1
        assert (await db.get_output(vid, lid))["strm_path"] == str(target / "TEST-001.strm")
        await db.close()
    asyncio.run(run())


def test_takeover_rechecks_external_duplicate_and_does_not_move(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        inbox = await inbox_movie(e, db, lid, vid, directory)
        p = await preview(e, lid, "takeover", images=False)
        directory.mkdir(parents=True)
        shutil.copyfile(inbox / "TEST-001.strm", directory / "renamed.strm")
        result = await execute(e, p)
        assert result["state"].get("failed") == 1
        assert (inbox / "TEST-001.strm").exists()
        await db.close()
    asyncio.run(run())


def test_failed_directory_is_explicit_and_scoped(make_store, boot, tmp_path, tmp_path_factory):
    async def run():
        e, db, lid, vid, directory = await seed(make_store, boot, tmp_path)
        failed = tmp_path_factory.mktemp("failed-scrapes")
        shutil.move(str(directory), str(failed / "TEST-001"))
        p = await preview(e, lid, "takeover", failed_dir=str(failed), images=False)
        assert p["state"].get("eligible") == 1
        await execute(e, p)
        assert not list(failed.rglob("*.strm"))
        with pytest.raises(ValueError, match="重叠"):
            await e.metadata.create(RepairRequest(library_id=lid, mode="takeover", failed_dir=str(e.writer.external_root(e.libs[lid]))))
        await db.close()
    asyncio.run(run())


def test_preview_failure_and_confirmation_guards(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, directory = await seed(make_store, boot, tmp_path)
        p = await preview(e, lid, "missing")
        with pytest.raises(ValueError):
            await e.metadata.approve(p["id"], "bad", True)
        with pytest.raises(ValueError):
            await e.metadata.approve(p["id"], p["state"]["preview_token"], False)
        jid = await e.metadata.approve(p["id"], p["state"]["preview_token"], True)
        assert await e.metadata.approve(p["id"], p["state"]["preview_token"], True) == jid
        root = e.writer.external_root(e.libs[lid])
        root.rename(root.with_name("offline"))
        failed = await preview(e, lid, "missing")
        assert not failed["state"]["ready"] and failed["state"]["error"]
        await db.close()
    asyncio.run(run())


def test_pause_cancel_and_image_failure_do_not_publish_partial_files(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, directory = await seed(make_store, boot, tmp_path)
        nfo = directory / "TEST-001.nfo"
        nfo.unlink()
        p = await preview(e, lid, "missing")
        jid = await e.metadata.approve(p["id"], p["state"]["preview_token"], True)
        for status in ("paused", "cancelled"):
            await db.update_job(jid, status=status)
            with pytest.raises(TaskStopped):
                await e.metadata.apply(await db.get_job(jid), {})
            assert not nfo.exists()
        await db.update_job(jid, status="running")
        e.fetcher.files["https://example.test/cover.jpg"] = b"not an image"
        await e.metadata.apply(await db.get_job(jid), {})
        assert (await db.get_job(jid))["state"].get("failed") == 1
        assert not nfo.exists() and not (directory / "TEST-001-fanart.jpg").exists()
        await db.update_job(jid, status="done")
        e.fetcher.files.clear()
        await e.metadata.retry(jid)
        await e.metadata.apply(await db.get_job(jid), {})
        assert (await db.get_job(jid))["state"].get("done") == 1
        assert nfo.exists()
        await db.close()
    asyncio.run(run())


def test_retry_recognizes_only_its_journaled_partial_temporary(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, _, directory = await seed(make_store, boot, tmp_path)
        (directory / "TEST-001.nfo").unlink()
        p = await preview(e, lid, "missing", images=False)
        original = metadata_files.publish

        def partial(path, prepared):
            Path(prepared["temporary"]).write_bytes(b"partial write")
            raise OSError("simulated crash during temporary write")

        monkeypatch.setattr(metadata_files, "publish", partial)
        result = await execute(e, p)
        assert result["state"].get("failed") == 1
        monkeypatch.setattr(metadata_files, "publish", original)
        await e.metadata.retry(result["id"])
        await e.metadata.apply(await db.get_job(result["id"]), {})
        assert (await db.get_job(result["id"]))["state"].get("done") == 1
        assert not list(directory.glob(".hls-metadata-*.tmp"))
        await db.close()
    asyncio.run(run())
