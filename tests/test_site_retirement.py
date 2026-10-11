import asyncio

import pytest

from hls2strm import archive, site_retirement
from hls2strm.engine import Engine
from hls2strm.observability import Metrics
from hls2strm.sites import SourceItem
from hls2strm.writer import OutputWriter

from .conftest import FakeFetcher
from .test_engine import model_fixture, wait_job


def test_retire_site_archives_and_restores(make_store, boot, tmp_path):
    """清退 Jable：别的站有能用的源的留下；只有 Jable 源的、别的源所在站点停用的下架，已整理的影片目录（nfo、图片）
    和收件目录里没整理的 strm 原样搬进归档目录；站点和订阅停用。恢复后原样搬回，站点和订阅重新启用。"""
    async def run():
        db, store = await make_store()
        html, ids = model_fixture()
        engine = Engine(db, FakeFetcher(html, ids), OutputWriter(store), store, Metrics(), boot)
        await engine.start()
        ext = tmp_path / "external"
        ext.mkdir()
        lid = (await engine.create_library("整理", "inbox", external_dir=str(ext)))["id"]
        await wait_job(db, await engine.create_crawl("/models/abc/", end_page=1, detail=False, library_id=lid))
        kept = await db.get_video("abf-156")
        await db.upsert_item("missav", SourceItem(key="abf-156", code="ABF-156", title="ABF-156"), "abf-156", engine._rank)
        await db.upsert_item("supjav", SourceItem(key="ipzz-983", code="IPZZ-983", title="IPZZ-983"), "ipzz-983",
                             engine._rank)
        await store.update({"sites": {"supjav": {"enabled": False}}})
        scraped = await db.get_video("ipzz-983")
        sub = await db.create_subscription(name="Jable 订阅", source="/models/abc/", library_id=lid, enabled=1)
        # 模拟外部刮削器：除了一部留在收件目录，都整理成「演员/番号/」并生成 nfo、图片
        inbox = store.output_dir / "inbox"
        unscraped = next(p for p in sorted(inbox.rglob("*.strm")) if p.stem not in ("ABF-156", "IPZZ-983"))
        for p in inbox.rglob("*.strm"):
            if p == unscraped:
                continue
            folder = ext / "演员" / p.stem
            folder.mkdir(parents=True)
            (folder / f"{p.stem}.nfo").write_text("metadata", encoding="utf-8")
            (folder / f"{p.stem}-poster.jpg").write_bytes(b"jpg")
            p.rename(folder / p.name)
        strm = ext / "演员" / "IPZZ-983" / "IPZZ-983.strm"

        plan = await site_retirement.preview(engine, "jable")
        assert plan["keep"] == 1 and plan["remove"] == plan["total"] - 1 == plan["in_libraries"]
        assert plan["reasons"]["别的站的源所在站点停用或没有启用的线路"] == 1
        assert "Jable 订阅" in plan["subscriptions"]
        assert strm.exists() and await db._one("SELECT 1 FROM site_retirements") is None  # 预览不动东西

        job = await wait_job(db, await site_retirement.apply(engine, "jable"))
        assert job["status"] == "done", await db.task_counts(job["id"])
        assert not store.current.site("jable").enabled and not (await db.get_subscription(sub))["enabled"]
        stash = boot.data_dir / "archive" / "整理"
        assert (stash / "external" / "演员" / "IPZZ-983" / "IPZZ-983.nfo").read_text(encoding="utf-8") == "metadata"
        assert (stash / "external" / "演员" / "IPZZ-983" / "IPZZ-983.strm").exists()
        assert not strm.parent.exists() and not unscraped.exists()
        assert (stash / "library" / unscraped.relative_to(inbox)).exists()
        assert (ext / "演员" / "ABF-156" / "ABF-156.strm").exists()
        assert (await db.get_video_by_id(kept["id"]))["status"] == "active"
        assert (await db.get_video_by_id(scraped["id"]))["status"] == "removed"
        assert await db.get_output(scraped["id"], lid) is None
        with pytest.raises(ValueError, match="已经清退"):
            await site_retirement.preview(engine, "jable")

        # 影片库里单独恢复源：Jable 还停用着，没有能用的源，影片仍然下架、文件不动
        await wait_job(db, await engine.create_source_removal([scraped["id"]]))
        assert (await db.get_video_by_id(scraped["id"]))["status"] == "removed" and not strm.exists()

        job = await wait_job(db, await site_retirement.restore(engine, "jable"))
        assert job["status"] == "done", await db.task_counts(job["id"])
        assert store.current.site("jable").enabled and (await db.get_subscription(sub))["enabled"]
        assert (strm.parent / "IPZZ-983.nfo").read_text(encoding="utf-8") == "metadata"
        assert "/play/ipzz-983.m3u8" in strm.read_text(encoding="utf-8")
        assert unscraped.exists()
        assert (await db.get_output(scraped["id"], lid))["strm_path"] == str(strm)
        assert (await db.get_video_by_id(scraped["id"]))["status"] == "active"
        assert not stash.exists() and await db._one("SELECT 1 FROM archives") is None
        assert await db._one("SELECT 1 FROM site_retirements") is None
        await engine.stop()

    asyncio.run(run())


def test_archive_plan_shared_dir(tmp_path):
    """整理目录里和别的片共用的目录：只搬这部片的 strm 和多画质版本，nfo、别的片不动；独占的目录整个搬。"""
    ext, root, dest = tmp_path / "ext", tmp_path / "inbox", tmp_path / "archive" / "库"
    shared = ext / "合集"
    shared.mkdir(parents=True)
    (shared / "AAA-001.strm").write_text("http://x/play/aaa-001.m3u8\n", encoding="utf-8")
    (shared / "AAA-001-720p.strm").write_text("http://x/play/aaa-001@720p.m3u8\n", encoding="utf-8")
    (shared / "AAA-001.nfo").write_text("nfo", encoding="utf-8")
    (shared / "BBB-002.strm").write_text("http://x/play/bbb-002.m3u8\n", encoding="utf-8")
    pairs = archive.plan([shared / "AAA-001.strm"], "aaa-001", root, ext, dest)
    assert sorted(pairs) == sorted([(str(shared / n), str(dest / "external" / "合集" / n))
                                    for n in ("AAA-001.strm", "AAA-001-720p.strm")])
    assert archive.stash(pairs) == 2
    assert (shared / "AAA-001.nfo").exists() and (shared / "BBB-002.strm").exists()
    assert archive.unstash(pairs, tmp_path / "archive") == []
    assert (shared / "AAA-001-720p.strm").exists() and not dest.exists()

    own = ext / "BBB-002"
    own.mkdir()
    (shared / "BBB-002.strm").rename(own / "BBB-002.strm")
    (own / "BBB-002.nfo").write_text("nfo", encoding="utf-8")
    assert archive.plan([own / "BBB-002.strm"], "bbb-002", root, ext, dest) == [
        (str(own), str(dest / "external" / "BBB-002"))]


def test_archive_dir_must_not_overlap(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        engine = Engine(db, FakeFetcher("", {}), OutputWriter(store), store, Metrics(), boot)
        await engine.reload_libraries()
        ext = tmp_path / "external"
        await engine.create_library("整理", "inbox", external_dir=str(ext))
        for bad in (ext / "archive", store.output_dir / "archive", tmp_path):
            with pytest.raises(ValueError, match="重叠"):
                engine.check_archive_dir(bad)
        engine.check_archive_dir(tmp_path / "archive")
        await store.update({"archive_dir": str(tmp_path / "归档")})
        with pytest.raises(ValueError, match="归档目录"):
            await engine.create_library("另一个", str(tmp_path / "归档" / "x"))

    asyncio.run(run())
