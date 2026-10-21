import asyncio
import errno
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hls2strm import maintenance_execute
from hls2strm.maintenance_plan import CleanupRequest
from hls2strm.maintenance_query import AssetFilter
from hls2strm.sites import SourceItem
from .test_maintenance import fixture, run_handler, NFO


def request(lid, **values):
    return CleanupRequest(action="unavailable", disposition="archive", query=AssetFilter(library_ids=[lid]), **values)


async def preview(e, lid):
    return await run_handler(e, "maintenance_preview", {"request": request(lid).model_dump()})


async def apply(e, plan):
    jid = await e.maintenance.approve(plan["id"], plan["state"]["preview_token"])
    await e.maintenance.apply(await e.db.get_job(jid), {})
    await e.db.update_job(jid, status="done")
    return await e.db.get_job(jid)


@pytest.mark.parametrize("status,has_output", [("gone", True), ("removed", False)])
def test_unavailable_archives_all_artifacts_and_inbox_without_scan(make_store, boot, tmp_path, status, has_output):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db._write("UPDATE sources SET status=? WHERE video_id=?", (status, vid))
            await db._write("UPDATE videos SET status=? WHERE id=?", (status, vid))
            if not has_output:
                await db.delete_output(vid, lid)
            # Scraper-renamed main file, multipart and quality variant, arbitrary artwork and metadata.
            (directory / "TEST-001.strm").rename(directory / "TEST0001-cd2.strm")
            (directory / "TEST0001-720p.strm").write_text("http://local/play/test-001@720p.m3u8", encoding="utf-8")
            (directory / "notes.json").write_text('{"keep":true}', encoding="utf-8")
            (directory / ".actors").mkdir()
            (directory / ".actors" / "actor.jpg").write_bytes(b"actor")
            inbox = e.writer.library_root(e.libs[lid])
            own = inbox / "TEST-001.strm"
            own.write_text("http://local/play/test-001.m3u8", encoding="utf-8")
            other, _ = await db.upsert_item("jable", SourceItem(key="test-002", code="TEST-002", title="keep"), "test-002", e._rank)
            other_file = inbox / "TEST-002.strm"
            other_file.write_text("http://local/play/test-002.m3u8", encoding="utf-8")
            before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
            before[own] = own.read_bytes()
            assert not await db._one("SELECT 1 FROM maintenance_roots")
            plan = await preview(e, lid)
            assert plan["state"]["eligible"] == 1, await e.maintenance.items(plan["id"])
            assert all(p.read_bytes() == content for p, content in before.items())
            assert not e.store.archive_dir.exists()  # Preview does not create an archive or touch media.
            files = await db._all("SELECT f.* FROM cleanup_files f JOIN maintenance_items i ON i.id=f.item_id WHERE i.preview_id=?", (plan["id"],))
            assert len(files) == len(before)
            assert all(Path(f["destination"]).is_relative_to(e.store.archive_dir) for f in files)
            job = await apply(e, plan)
            assert not job["state"].get("failed"), await e.maintenance.items(job["id"])
            assert job["state"]["trashed_files"] == len(before)
            assert all(not p.exists() for p in before) and other_file.is_file()
            assert await db.get_output(vid, lid) is None and await db.collection_blocked(vid, lid)
            assert not await db.collection_blocked(other, lid)
            assert "expires_at" not in job["params"]
            assert not (await e.maintenance.purge_expired())["expired_files"]
            restoration = await run_handler(e, "maintenance_restore_preview", {"restore_of": job["id"]})
            jid = await e.maintenance.approve(restoration["id"], restoration["state"]["preview_token"])
            await e.maintenance.restore(await db.get_job(jid), {})
            assert not (await db.get_job(jid))["state"].get("failed"), await e.maintenance.items(jid)
            assert all(p.read_bytes() == content for p, content in before.items())
            assert not await db.collection_blocked(vid, lid)
            assert bool(await db.get_output(vid, lid)) == has_output
        finally:
            await db.close()
    asyncio.run(run())


def test_enabled_alternative_keeps_video_and_disabled_site_is_unavailable(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            await db.upsert_item("missav", SourceItem(key="test-001", code="TEST-001", title="keep"), "test-001", e._rank)
            assert not (await preview(e, lid))["state"].get("eligible")
            await e.store.update({"sites": {"missav": {"enabled": False}}})
            plan = await preview(e, lid)
            assert plan["state"]["eligible"] == 1
            # Enabling a source after the preview must stop execution.
            await e.store.update({"sites": {"missav": {"enabled": True}}})
            job = await apply(e, plan)
            assert job["state"]["failed"] == 1
            assert (directory / "TEST-001.strm").is_file()
            assert await db.get_output(vid, lid) and not await db.collection_blocked(vid, lid)
        finally:
            await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["file", "source", "copy", "output"])
def test_changed_evidence_is_not_archived(make_store, boot, tmp_path, change):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            plan = await preview(e, lid)
            if change == "file":
                (directory / "poster.jpg").write_bytes(b"changed")
            elif change == "source":
                await db.upsert_item("missav", SourceItem(key="test-001", code="TEST-001", title="new"), "test-001", e._rank)
            elif change == "copy":
                other = directory.parent / "duplicate"
                other.mkdir()
                (other / "renamed.strm").write_text("http://local/play/test-001.m3u8", encoding="utf-8")
            else:
                await db.set_output(vid, lid, str(directory / "different.strm"))
            job = await apply(e, plan)
            assert job["state"]["failed"] == 1
            assert (directory / "TEST-001.strm").is_file()
            assert not await db.collection_blocked(vid, lid)
        finally:
            await db.close()
    asyncio.run(run())


def test_shared_directory_is_reported_and_never_partially_cleaned(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            await db.upsert_item("jable", SourceItem(key="test-002", code="TEST-002", title="keep"), "test-002", e._rank)
            (directory / "TEST-002.strm").write_text("http://local/play/test-002.m3u8", encoding="utf-8")
            plan = await preview(e, lid)
            assert not plan["state"].get("eligible") and plan["state"]["skipped"] == 1
            items = await e.maintenance.items(plan["id"])
            assert "共享" in items["items"][0]["error"]
            assert (directory / "TEST-001.strm").is_file() and (directory / "poster.jpg").is_file()
        finally:
            await db.close()
    asyncio.run(run())


def test_line_policy_and_retired_site_are_checked_locally(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            await db.upsert_item("javmost", SourceItem(key="test-001", code="TEST-001", title="lines"), "test-001", e._rank)
            source = next(s for s in await db.get_sources(vid) if s["site"] == "javmost")
            await db.set_lines(source["id"], [("MOST", "https://invalid.example/unsupported")])
            assert (await preview(e, lid))["state"]["eligible"] == 1
            await db.set_lines(source["id"], [("DOO", "https://invalid.example/supported")])
            assert not (await preview(e, lid))["state"].get("eligible")
            await db._write("INSERT INTO site_retirements(site,job_id,created_at) VALUES('javmost',0,0)")
            plan = await preview(e, lid)
            assert plan["state"]["eligible"] == 1
            items = await e.maintenance.items(plan["id"])
            assert "清退残留" in items["items"][0]["data"]["reason"]
            assert e.fetcher.calls == []
            assert (directory / "TEST-001.strm").exists()
        finally:
            await db.close()
    asyncio.run(run())


@pytest.mark.parametrize("problem", ["entity_media", "target_conflict", "archive_mount"])
def test_unsafe_archive_preserves_originals(make_store, boot, tmp_path, problem):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            if problem == "entity_media":
                (directory / "unrelated.mkv").write_bytes(b"real media")
            if problem == "archive_mount":
                e.store.archive_dir.mkdir()
            plan = await preview(e, lid)
            if problem == "entity_media":
                assert not plan["state"].get("eligible") and plan["state"]["skipped"] == 1
                assert (directory / "unrelated.mkv").read_bytes() == b"real media"
                return
            if problem == "target_conflict":
                file = await db._one("SELECT destination FROM cleanup_files f JOIN maintenance_items i ON i.id=f.item_id WHERE i.preview_id=? ORDER BY f.id LIMIT 1", (plan["id"],))
                target = Path(file["destination"])
                target.parent.mkdir(parents=True)
                target.write_bytes(b"do not overwrite")
            else:
                e.store.archive_dir.rename(e.store.archive_dir.with_name("original-archive"))
                e.store.archive_dir.mkdir()
            job = await apply(e, plan)
            assert job["state"]["failed"] == 1
            assert (directory / "TEST-001.strm").is_file() and (directory / "poster.jpg").read_bytes() == b"poster"
            assert not await db.collection_blocked(vid, lid)
            if problem == "target_conflict":
                assert target.read_bytes() == b"do not overwrite"
        finally:
            await db.close()
    asyncio.run(run())


def test_metadata_only_residue_requires_unique_identity(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            (directory / "TEST-001.strm").unlink()
            await db.delete_output(vid, lid)
            plan = await preview(e, lid)
            assert plan["state"]["eligible"] == 1, await e.maintenance.items(plan["id"])
            await db.upsert_item("jable", SourceItem(key="test-001-u", code="TEST-001", title="U", uncensored=True), "test-001-u", e._rank)
            ambiguous = await preview(e, lid)
            assert not ambiguous["state"].get("eligible") and ambiguous["state"]["unknown"] >= 1
            assert (directory / "TEST-001.nfo").read_text(encoding="utf-8") == NFO
        finally:
            await db.close()
    asyncio.run(run())


def test_missing_root_and_unsafe_scope_never_produce_ready_preview(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            directory.parents[1].rename(tmp_path / "unmounted")
            with pytest.raises((ValueError, OSError)):
                await preview(e, lid)
            job = await db._one("SELECT state FROM jobs WHERE kind='maintenance_preview' ORDER BY id DESC LIMIT 1")
            assert not json.loads(job["state"]).get("ready")
            assert not e.store.archive_dir.exists()
        finally:
            await db.close()
    asyncio.run(run())
    for values in ({"scope": "global"}, {"stop_collection": False}, {"disposition": "permanent"},
                   {"query": {"categories": ["example"]}}):
        with pytest.raises(ValidationError):
            CleanupRequest.model_validate({"action": "unavailable", "disposition": "archive", **values})


def test_cross_device_archive_and_recovery_after_journal_interruption(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db.mark_source_gone("jable", "test-001")
            plan = await preview(e, lid)
            move = maintenance_execute.no_replace
            def cross_device(source, destination):
                if not Path(source).name.startswith(".hls2strm-"):
                    raise OSError(errno.EXDEV, "simulated cross-device move")
                return move(source, destination)
            monkeypatch.setattr(maintenance_execute, "no_replace", cross_device)
            original_write = db._write
            injected = False
            async def fail(sql, args=(), **kwargs):
                nonlocal injected
                if not injected and sql.startswith("UPDATE cleanup_files SET state=?,error=") and args[0] == "trashed":
                    injected = True
                    raise RuntimeError("interrupted after moving file")
                return await original_write(sql, args, **kwargs)
            monkeypatch.setattr(db, "_write", fail)
            jid = await e.maintenance.approve(plan["id"], plan["state"]["preview_token"])
            with pytest.raises(RuntimeError):
                await e.maintenance.apply(await db.get_job(jid), {})
            monkeypatch.setattr(db, "_write", original_write)
            await e.maintenance.apply(await db.get_job(jid), {})
            job = await db.get_job(jid)
            assert not job["state"].get("failed"), await e.maintenance.items(jid)
            assert job["state"]["trashed_files"] == 7
            await db.update_job(jid, status="done")
            restoration = await run_handler(e, "maintenance_restore_preview", {"restore_of": jid})
            restore_id = await e.maintenance.approve(restoration["id"], restoration["state"]["preview_token"])
            await e.maintenance.restore(await db.get_job(restore_id), {})
            assert not (await db.get_job(restore_id))["state"].get("failed"), await e.maintenance.items(restore_id)
            assert (directory / "TEST-001.nfo").read_text(encoding="utf-8") == NFO
        finally:
            await db.close()
    asyncio.run(run())
