import asyncio
from collections import Counter
from pathlib import Path

import pytest

from hls2strm.storage import delete_batch, safe_directory, scan_directories
from .conftest import FakeFetcher
from .test_engine import build, wait_job


def movie(root):
    directory = root / "767GPH" / "767GPH-008"
    extra = directory / "extrafanart"
    extra.mkdir(parents=True)
    for n in [1, 2, 3, 10, 11]:
        (extra / f"fanart{n}.jpg").write_bytes(bytes(n))
    for name in ["767GPH-008.strm", "767GPH-008.nfo", "poster.jpg", "fanart.jpg", "thumb.jpg"]:
        (directory / name).write_bytes(b"keep")
    (extra / "notes.txt").write_bytes(b"keep")
    (extra / "nested").mkdir()
    (extra / "nested" / "keep.jpg").write_bytes(b"keep")
    return directory, extra


@pytest.mark.parametrize("keep,removed", [(0, 5), (2, 3), (20, 0)])
def test_statistics_and_natural_retention(tmp_path, keep, removed):
    directory, extra = movie(tmp_path)
    trash = tmp_path / ".hls2strm-trash" / "extrafanart"
    trash.mkdir(parents=True)
    (trash / "fanart1.jpg").write_bytes(b"trash")
    rows = list(scan_directories(tmp_path, keep))
    counts = Counter()
    for row in rows:
        counts.update(row["counts"])
    assert counts["bytes"] == 55 and counts["extra_bytes"] == 35
    assert counts["delete_files"] == removed
    result = delete_batch(tmp_path, safe_directory(tmp_path), rows)
    assert result["deleted"] == removed
    if keep == 2:
        assert sorted(p.name for p in extra.glob("*.jpg")) == ["fanart1.jpg", "fanart2.jpg"]
        assert result["deleted_bytes"] == 24
    assert (extra / "notes.txt").read_bytes() == b"keep"
    assert (extra / "nested" / "keep.jpg").read_bytes() == b"keep"
    assert (trash / "fanart1.jpg").read_bytes() == b"trash"
    for name in ["767GPH-008.strm", "767GPH-008.nfo", "poster.jpg", "fanart.jpg", "thumb.jpg"]:
        assert (directory / name).read_bytes() == b"keep"


def test_changed_missing_and_new_files_are_preserved(tmp_path):
    _, extra = movie(tmp_path)
    rows = list(scan_directories(tmp_path, 0))
    (extra / "fanart1.jpg").write_bytes(b"changed")
    (extra / "fanart2.jpg").unlink()
    (extra / "fanart20.jpg").write_bytes(b"new")
    result = delete_batch(tmp_path, safe_directory(tmp_path), rows)
    assert result["deleted"] == 3 and result["skipped"] == 2
    assert (extra / "fanart1.jpg").read_bytes() == b"changed"
    assert (extra / "fanart20.jpg").read_bytes() == b"new"
    # 重复执行不会扩大范围。
    assert delete_batch(tmp_path, safe_directory(tmp_path), rows)["deleted"] == 0


def test_replaced_directory_skipped(tmp_path):
    _, extra = movie(tmp_path)
    rows = list(scan_directories(tmp_path, 0))
    extra.rename(extra.with_name("saved"))
    extra.mkdir()
    (extra / "fanart1.jpg").write_bytes(b"new")
    result = delete_batch(tmp_path, safe_directory(tmp_path), rows)
    assert result["deleted"] == 0 and result["skipped"] == 5
    assert (extra / "fanart1.jpg").read_bytes() == b"new"


def test_root_identity_and_links(tmp_path):
    root = tmp_path / "root"
    _, extra = movie(root)
    rows = list(scan_directories(root, 0))
    original = safe_directory(root)
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(ValueError, match="根目录已改变"):
        delete_batch(root, original, rows)
    try:
        (root / "link").symlink_to(tmp_path / "old", target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不支持创建符号链接")
    assert sum(len(row["files"]) for row in scan_directories(root, 0)) == 0
    with pytest.raises(ValueError, match="链接"):
        safe_directory(root / "link")


def test_unlink_error_does_not_stop_other_files(tmp_path, monkeypatch):
    _, extra = movie(tmp_path)
    rows = list(scan_directories(tmp_path, 0))
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == "fanart1.jpg":
            raise PermissionError("read only")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = delete_batch(tmp_path, safe_directory(tmp_path), rows)
    assert result["failed"] == 1 and result["deleted"] == 4
    assert (extra / "fanart1.jpg").exists()


def test_persisted_preview_confirmation_and_cleanup(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.start()
        try:
            root = tmp_path / "organized"
            _, extra = movie(root)
            library_id = (await engine.create_library("外部", "inbox", external_dir=str(root)))["id"]
            scan = await wait_job(db, await engine.storage.create(library_id, 2))
            assert scan["status"] == "done" and scan["state"]["summary"]["counts"]["delete_files"] == 3
            assert len(list(extra.glob("*.jpg"))) == 5  # 预览无副作用
            with pytest.raises(ValueError, match="预览无效"):
                await engine.storage.approve(library_id, scan["id"], "wrong")
            with pytest.raises(ValueError, match="预览无效"):
                await engine.storage.approve(1, scan["id"], scan["state"]["preview_token"])
            # 模拟管理器重建：执行依赖持久化清单，不依赖内存预览。
            from hls2strm.storage import Storage
            engine.storage = Storage(engine, boot.data_dir)
            token = scan["state"]["preview_token"]
            job_id = await engine.storage.approve(library_id, scan["id"], token)
            assert await engine.storage.approve(library_id, scan["id"], token) == job_id
            job = await wait_job(db, job_id)
            assert job["state"]["deleted"] == 3 and job["state"]["deleted_bytes"] == 24
            assert len(list(extra.glob("*.jpg"))) == 2
            scan = await wait_job(db, await engine.storage.create(library_id, 0))
            engine.libs[library_id]["external_dir"] = str(tmp_path / "changed")
            with pytest.raises(ValueError, match="库目录已改变"):
                await engine.storage.approve(library_id, scan["id"], scan["state"]["preview_token"])
        finally:
            await engine.stop()
            await db.close()
    asyncio.run(run())


def test_cleanup_resumes_frozen_batches(make_store, boot, tmp_path):
    async def run():
        from hls2strm.engine import TaskStopped
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()  # 手动执行 handler，精确控制批次间的暂停。
        try:
            root = engine.storage.root(1)
            for n in range(101):
                movie(root / str(n))
            scan_id = await engine.storage.create(1, 0)
            await engine.storage.scan(await db.get_job(scan_id), {})
            await db.update_job(scan_id, status="done")
            scan = await db.get_job(scan_id)
            job_id = await engine.storage.approve(1, scan_id, scan["state"]["preview_token"])
            original = engine.checkpoint
            calls = 0
            async def pause(job_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise TaskStopped("pending")
                await original(job_id)
            engine.checkpoint = pause
            with pytest.raises(TaskStopped):
                await engine.storage.clean(await db.get_job(job_id), {})
            job = await db.get_job(job_id)
            assert job["state"]["cursor"] == 100 and job["state"]["deleted"] == 500
            remaining = [p for p in root.rglob("fanart1.jpg")]
            assert len(remaining) == 1
            new = remaining[0].with_name("fanart99.jpg")
            new.write_bytes(b"new after preview")
            engine.checkpoint = original
            await engine.storage.clean(job, {})
            final = await db.get_job(job_id)
            assert final["state"]["deleted"] == 505 and final["state"]["cursor"] == 101
            assert new.read_bytes() == b"new after preview"
        finally:
            await db.close()
    asyncio.run(run())
