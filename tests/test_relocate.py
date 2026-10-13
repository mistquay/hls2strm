import asyncio
import os
from pathlib import Path

import pytest

from hls2strm.trash import TRASH_DIR
from .conftest import FakeFetcher
from .test_engine import build, wait_job
from .test_operations import prepare, seed
from .test_trash import scraped


async def setup(make_store, boot, tmp_path, *, dst_excludes=False):
    """「破解」和「其他」两个外部整理库，「其他」排除「破解」；两部片已写进「其他」，并被 mdcng 整理成 前缀/番号/。"""
    db, store = await make_store()
    await store.update({"download_cover": False, "quality_capture": False, "relocate_scraped": True})
    ids = [await seed(db, store, key) for key in ("abc-001", "abc-002")]
    engine = build(db, store, boot, FakeFetcher("", {}))
    await engine.start()
    mdc = tmp_path / "mdc"
    (mdc / "dst").mkdir(parents=True)
    blocker = (await engine.create_library("挡板", "blocker"))["id"]
    dst = (await engine.create_library("破解", "dst", external_dir=str(mdc / "dst"),
                                       excludes=[blocker] if dst_excludes else []))["id"]
    src = (await engine.create_library("其他", "src", external_dir=str(mdc / "src"), excludes=[dst]))["id"]
    for vid in ids:
        await db.ensure_output(vid, src)
        await engine._output_one(await db.get_video_by_id(vid), src, cover=False, old_strm="", settle=True)
    for p in list((store.output_dir / "src").rglob("*.strm")):
        code = p.stem.upper()
        d = scraped(mdc / "src" / code.split("-")[0] / code, code)
        p.rename(d / p.name)
    await engine.strm.locate(engine._library(src))  # 线上「同步位置」定时把记录的路径更新到整理后的位置
    return db, store, engine, ids, src, dst, mdc


def strms(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.strm")) if root.exists() else []


def test_moving_to_excluded_library_relocates_scraped_dir(make_store, boot, tmp_path):
    """写进「破解」时，「其他」里整理好的目录整个搬到「破解」的整理目录同样位置：不进回收区，不往收件目录写新 strm。"""
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            await engine.add_to_library([a], dst)
            moved = mdc / "dst" / "ABC" / "ABC-001"
            assert (moved / "ABC-001.strm").is_file() and (moved / "ABC-001.nfo").is_file()
            assert (moved / "extrafanart" / "fanart1.jpg").is_file()
            assert not (mdc / "src" / "ABC" / "ABC-001").exists() and (mdc / "src" / "ABC" / "ABC-002").is_dir()
            assert strms(store.output_dir / "dst") == [] and not (mdc / "src" / TRASH_DIR).exists()
            assert await db.get_output(a, src) is None
            assert (await db.get_output(a, dst))["strm_path"] == str(moved / "ABC-001.strm")
            assert (await engine.settle()).get("removed", 0) == 0  # 「其他」那条已经没了，归并不用再移出
            assert (moved / "ABC-001.strm").read_text().strip().endswith("/play/abc-001.m3u8")
        finally:
            await engine.stop()
    asyncio.run(run())


def test_removal_relocates_when_target_is_waiting(make_store, boot, tmp_path):
    """「破解」有排除库，新加入的先等归并；这时从「其他」移出，整理好的目录搬过去，归并不再往收件目录写。"""
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path, dst_excludes=True)
        try:
            await engine.add_to_library([a], dst)
            assert (await db.get_output(a, dst))["strm_path"] == ""
            await engine.remove_from_library([a], src)
            moved = mdc / "dst" / "ABC" / "ABC-001" / "ABC-001.strm"
            assert moved.is_file() and (await db.get_output(a, dst))["strm_path"] == str(moved)
            await engine.settle()
            assert strms(store.output_dir / "dst") == [] and not (mdc / "src" / TRASH_DIR).exists()
        finally:
            await engine.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["shared", "disabled", "inbox"])
def test_falls_back_to_trash_and_rewrite(make_store, boot, tmp_path, case):
    """共用目录、目标已有同名目录、关掉了、还没整理的：照旧删旧写新（独占的整理目录进回收区）。"""
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            folder = mdc / "src" / "ABC" / "ABC-001"
            if case == "shared":
                (mdc / "src" / "ABC" / "ABC-002" / "ABC-002.strm").rename(folder / "ABC-002.strm")
                await engine.strm.locate(engine._library(src))
            elif case == "target_exists":
                (mdc / "dst" / "ABC" / "ABC-001").mkdir(parents=True)
            elif case == "disabled":
                await store.update({"relocate_scraped": False})
            else:  # mdcng 还没整理：strm 放回收件目录
                inbox = store.output_dir / "src" / "ABC-001"
                inbox.mkdir(parents=True, exist_ok=True)
                (folder / "ABC-001.strm").rename(inbox / "ABC-001.strm")
                await engine.strm.locate(engine._library(src))
            await engine.add_to_library([a], dst)
            assert strms(store.output_dir / "dst") == [os.path.join("ABC-001", "ABC-001.strm")]
            assert (await engine.settle())["removed"] == 1
            assert not list((mdc / "dst").rglob("*.strm"))
            if case == "shared":
                assert (folder / "ABC-002.strm").is_file() and not (folder / "ABC-001.strm").exists()
            elif case != "inbox":
                assert not folder.exists() and (mdc / "src" / TRASH_DIR).is_dir()
        finally:
            await engine.stop()
    asyncio.run(run())


def test_library_rule_change_relocates_and_previews(make_store, boot, tmp_path):
    """给「破解」加规则：预览数出要搬的目录；执行时先加入再移出，整理好的目录搬过去。"""
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            old = await db.get_library(dst)
            values = {k: old[k] for k in ("name", "dir", "external_dir", "path_template", "versions", "rule", "sources",
                                          "excludes")}
            values["rule"] = {"keywords": ["ABC-001"]}
            preview = await prepare(engine, {"kind": "library", "payload": {"library_id": dst, "values": values}})
            counts = preview["state"]["_preview"]["counts"]
            assert (counts["added"], counts["removed"], counts["relocated"]) == (1, 1, 1)
            job = await engine.operations.approve(preview["id"], preview["state"]["preview_token"])
            done = await wait_job(db, job)
            assert not done["state"].get("partial_failure"), await db.list_tasks(job)
            moved = mdc / "dst" / "ABC" / "ABC-001" / "ABC-001.strm"
            assert moved.is_file() and (await db.get_output(a, dst))["strm_path"] == str(moved)
            assert await db.get_output(a, src) is None and (await db.get_output(b, src))["strm_path"]
            assert strms(store.output_dir / "dst") == [] and not (mdc / "src" / TRASH_DIR).exists()
        finally:
            await engine.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["target", "offline", "move"])
def test_relocation_failure_keeps_scraped_files(make_store, boot, tmp_path, monkeypatch, failure):
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            folder = mdc / "src" / "ABC" / "ABC-001"
            if failure == "target":
                (mdc / "dst" / "ABC" / "ABC-001").mkdir(parents=True)
            elif failure == "offline":
                (mdc / "dst").rmdir()
            else:
                def fail(*args):
                    raise OSError("disk full")
                monkeypatch.setattr("hls2strm.relocate.archive.move", fail)
            with pytest.raises((ValueError, OSError)):
                await engine.add_to_library([a], dst)
            assert (folder / "ABC-001.strm").exists() and (folder / "ABC-001.nfo").exists()
            assert await db.get_output(a, src)
            assert not (mdc / "src" / TRASH_DIR).exists()
        finally:
            await engine.stop()
    asyncio.run(run())


def test_resume_after_files_moved_before_database_write(make_store, boot, tmp_path, monkeypatch):
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            real = db.set_output
            async def fail(*args, **kwargs):
                raise OSError("interrupted before saving path")
            monkeypatch.setattr(db, "set_output", fail)
            with pytest.raises(OSError):
                await engine.add_to_library([a], dst)
            assert not (mdc / "src" / "ABC" / "ABC-001").exists()
            moved = mdc / "dst" / "ABC" / "ABC-001" / "ABC-001.strm"
            assert moved.exists() and await db.get_output(a, src)
            monkeypatch.setattr(db, "set_output", real)
            await engine._output_one(await db.get_video_by_id(a), dst, cover=False, settle=True)
            assert (await db.get_output(a, dst))["strm_path"] == str(moved)
            assert await db.get_output(a, src) is None
            assert strms(store.output_dir / "dst") == []
        finally:
            await engine.stop()
    asyncio.run(run())


def test_waiting_exclusions_do_not_move_scraped_directory(make_store, boot, tmp_path, monkeypatch):
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path, dst_excludes=True)
        try:
            await engine.add_to_library([a], dst)
            monkeypatch.setattr(engine, "_seen_before", lambda *args: 0)
            with pytest.raises(ValueError, match="等待排除库"):
                await engine.remove_from_library([a], src)
            assert (mdc / "src" / "ABC" / "ABC-001" / "ABC-001.strm").exists()
            assert await db.get_output(a, src)
        finally:
            await engine.stop()
    asyncio.run(run())


def test_relocation_preserves_quality_files_and_metadata(make_store, boot, tmp_path):
    async def run():
        db, store, engine, (a, b), src, dst, mdc = await setup(make_store, boot, tmp_path)
        try:
            folder = mdc / "src" / "ABC" / "ABC-001"
            version = folder / "ABC-001 - 720p.strm"
            version.write_text("http://fixture/play/abc-001@720p.m3u8", encoding="utf-8")
            before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
            await engine.add_to_library([a], dst)
            moved = mdc / "dst" / "ABC" / "ABC-001"
            after = {str(p.relative_to(moved)): p.read_bytes() for p in moved.rglob("*") if p.is_file()}
            assert after == before
            assert not folder.exists()
        finally:
            await engine.stop()
    asyncio.run(run())
