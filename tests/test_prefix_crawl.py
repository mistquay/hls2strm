import asyncio

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.codes import code_key, series_prefix
from hls2strm.config import BootConfig, SettingsStore
from hls2strm.db import Database
from hls2strm.prefix_crawl import cancel, create, do_list
from hls2strm.prefix_store import normalize_prefixes, progress, snapshot, statistics
from hls2strm.sites import SITES, SourceItem, ListPage

from .conftest import FakeFetcher
from .test_engine import build, wait_job


def listing(*keys, last=1):
    return '<html><body>' + ''.join(f'<div class="thumbnail"><div class="my-2"><a href="/cn/{key}">{key.upper()}</a></div></div>' for key in keys) + f'<a href="?page={last}">last</a></body></html>'


def body(scope_id, **kwargs):
    return {"scope_id": scope_id, "prefixes": ["ABC"], "sites": ["missav"], "mode": "expand",
            "library_id": 1, "max_pages": 0, "strict": True, "detail": False, **kwargs}


async def setup(make_store, boot, keys=("ABC-001",)):
    db, store = await make_store()
    for code in keys:
        await db.upsert_item("javmost", SourceItem(key=code.lower(), code=code, title=code), code.lower(), lambda _: 0)
    fetcher = FakeFetcher("", {})
    fetcher.pages["missav"] = {}
    engine = build(db, store, boot, fetcher)
    await engine.reload_libraries()
    return db, engine, fetcher, await snapshot(db, {})


@pytest.mark.parametrize("code,expected", [
    ("JUFE-001", "JUFE"), ("jufe001", "JUFE"), ("259LUXU-1850", "LUXU"),
    ("300MIUM-001", "MIUM"), ("3DSVR-001", "3DSVR"), ("T28-123", "T28"),
    ("FC2-PPV-1234567", "FC2PPV"), ("HEYZO-1234", "HEYZO"),
    ("n1234", "TOKYOHOT"), ("010120-001", ""), ("100826_001", ""),
    ("1PON-100826_001", ""), ("unknown", ""), ("ABC", ""),
])
def test_series_prefix(code, expected):
    assert series_prefix(code_key(code)) == expected


def test_manual_aliases_and_validation():
    assert normalize_prefixes(["luxu", "259LUXU", "fc2", "FC2-PPV"]) == ["FC2PPV", "LUXU"]
    with pytest.raises(ValueError):
        normalize_prefixes(["../../foo"])


def test_statistics_deduplicate_sources_and_freeze_scope(make_store, boot):
    async def run():
        db, engine, _, _ = await setup(make_store, boot, ("ABC-001", "ABC-002", "259LUXU-001", "010120-001"))
        video = await db.get_video("abc-001")
        await db.upsert_item("missav", SourceItem(key="abc-001", code="ABC-001", title=""), "abc-001", lambda _: 0)
        scope_id = await snapshot(db, {"has_site": ["javmost"], "q": "ABC"})
        stats = await statistics(db, scope_id, "missav")
        assert stats["total"] == 2 and stats["items"][0]["covered"] == 1 and stats["items"][0]["missing"] == 1
        await db.upsert_item("javmost", SourceItem(key="abc-003", code="ABC-003", title=""), "abc-003", lambda _: 0)
        assert (await statistics(db, scope_id, "jable"))["total"] == 2
        selected = await snapshot(db, {}, [video["id"], video["id"]])
        assert (await statistics(db, selected, "missav"))["total"] == 1
        whole = await snapshot(db, {})
        all_stats = await statistics(db, whole, "missav")
        assert all_stats["unrecognized"] == 1 and any(r["prefix"] == "LUXU" for r in all_stats["items"])
    asyncio.run(run())


def test_interrupted_creation_can_finish_and_partial_duplicate_is_rejected(make_store, boot, monkeypatch):
    async def run():
        db, engine, _, scope_id = await setup(make_store, boot)
        site = SITES["missav"]
        original = site.prefix_source
        def fail(_):
            raise ValueError("模拟创建计划时中断")
        monkeypatch.setattr(site, "prefix_source", fail)
        with pytest.raises(ValueError):
            await create(engine, body(scope_id))
        incomplete = (await db.list_jobs())[0]
        assert incomplete["status"] == "paused" and not await db.list_tasks(incomplete["id"])
        monkeypatch.setattr(site, "prefix_source", original)
        assert await create(engine, body(scope_id)) == incomplete["id"]
        assert len(await db.list_tasks(incomplete["id"])) == 1
        with pytest.raises(ValueError, match="已有进行中的任务"):
            await create(engine, body(scope_id, prefixes=["ABC", "DEF"]))
    asyncio.run(run())


def test_expand_full_scan_existing_membership_and_replay(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        original = await db.get_video("abc-001")
        fetcher.pages["missav"] = {"/cn/search/ABC?page=1": listing("abc-001", "xyz-001", last=2),
                                    "/cn/search/ABC?page=2": listing("abc-002", "abc-001", last=2)}
        jid = await create(engine, body(scope_id))
        assert await create(engine, body(await snapshot(db, {}))) == jid
        await engine.start()
        try:
            job = await wait_job(db, jid)
        finally:
            await engine.stop()
        assert job["state"]["list_complete"] and not job["state"]["limited"]
        assert await db.find_source("missav", "abc-001")
        assert not await db.get_output(original["id"], 1)  # 原影片没有输出库，不强行归入。
        new = await db.get_video("abc-002")
        assert (await db.get_output(new["id"], 1))["strm_path"]
        assert not await db.get_video("xyz-001")
        summary = (await progress(db, jid))["summary"]
        assert summary == {"seen": 2, "new": 1, "sources_added": 2, "skipped": 1}
        # 模拟同一已完成页重跑，分类和新增源计数保持首次结果。
        await db.update_job(jid, status="running")
        task = next(t for t in await db.list_tasks(jid) if t["target"].endswith("/2"))
        await do_list(engine, await db.get_job(jid), task)
        assert (await progress(db, jid))["summary"] == summary
        assert len(await db.list_tasks(jid)) == 2
    asyncio.run(run())


def test_existing_mode_does_not_expand_or_attach_other_edition(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        # 快照建立后新增的同前缀影片仍不在处理范围内。
        await db.upsert_item("javmost", SourceItem(key="abc-002", code="ABC-002", title=""), "abc-002", lambda _: 0)
        fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001", "abc-001-chinese-subtitle", "abc-001-uncensored-leak", "abc-002", "abc-003")
        jid = await create(engine, body(scope_id, mode="existing", library_id=None))
        await engine.start()
        try:
            await wait_job(db, jid)
        finally:
            await engine.stop()
        assert await db.find_source("missav", "abc-001-chinese-subtitle")
        assert not await db.find_source("missav", "abc-001-uncensored-leak")
        assert not await db.find_source("missav", "abc-002")
        assert not await db.get_video("abc-003")
        assert (await progress(db, jid))["summary"] == {"seen": 1, "new": 0, "sources_added": 2, "skipped": 3}
    asyncio.run(run())


def test_multiple_target_sites_share_identity_and_deduplicate_new_films(make_store, boot, monkeypatch):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001", "abc-002")
        monkeypatch.setattr(SITES["jable"], "parse_list", lambda _: ListPage([
            SourceItem(key="abc-001", code="ABC-001", title="ABC-001"),
            SourceItem(key="abc-002", code="ABC-002", title="ABC-002"),
        ], 1))
        jid = await create(engine, body(scope_id, sites=["jable", "missav"]))
        await engine.start()
        try:
            await wait_job(db, jid)
        finally:
            await engine.stop()
        result = await progress(db, jid)
        assert len(result["items"]) == 2
        assert result["summary"] == {"seen": 2, "new": 1, "sources_added": 4, "skipped": 0}
        assert len(await db.get_sources((await db.get_video("abc-002"))["id"])) == 2
    asyncio.run(run())


def test_removed_source_is_not_restored(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        await db.upsert_item("missav", SourceItem(key="abc-001", code="ABC-001", title=""), "abc-001", lambda _: 0)
        await db._write("UPDATE sources SET status='removed' WHERE site='missav'")
        fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001")
        jid = await create(engine, body(scope_id))
        await engine.start()
        try:
            await wait_job(db, jid)
        finally:
            await engine.stop()
        assert (await db.find_source("missav", "abc-001"))["status"] == "removed"
        row = (await progress(db, jid))["items"][0]
        assert row["sources_added"] == 0 and row["removed"] == 1
    asyncio.run(run())


def test_page_limit_and_prefix_cancel(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001", last=10)
        jid = await create(engine, body(scope_id, max_pages=1))
        await engine.start()
        try:
            job = await wait_job(db, jid)
        finally:
            await engine.stop()
        assert job["state"]["limited"] and (await progress(db, jid))["items"][0]["scan_status"] == "limited"
        other = await create(engine, body(scope_id))
        await cancel(engine, other, "missav", "ABC")
        assert (await db.get_job(other))["status"] == "done"
        assert (await progress(db, other))["items"][0]["scan_status"] == "cancelled"
        await engine.set_job_status(other, "retry")
        assert not await db.has_open_tasks(other)  # 重试不恢复用户取消项。
    asyncio.run(run())


def test_failure_retry_resumes_page_and_preserves_count(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001", last=2)
        jid = await create(engine, body(scope_id))
        await engine.start()
        try:
            job = await wait_job(db, jid)
            assert job["state"]["partial_failure"]
            assert (await progress(db, jid))["items"][0]["scan_status"] == "failed"
            fetcher.pages["missav"]["/cn/search/ABC?page=2"] = listing("abc-002", last=2)
            await engine.set_job_status(jid, "retry")
            done = await wait_job(db, jid)
            assert done["state"]["list_complete"] and not done["state"].get("partial_failure")
        finally:
            await engine.stop()
        assert (await progress(db, jid))["summary"]["sources_added"] == 2
    asyncio.run(run())


def test_unrecognized_empty_page_is_failure_not_complete_or_negative_cache(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        fetcher.pages["missav"] = {"/cn/search/ABC?page=1": listing("abc-001", last=2),
                                    "/cn/search/ABC?page=2": "<html>unexpected page</html>"}
        jid = await create(engine, body(scope_id))
        await engine.start()
        try:
            job = await wait_job(db, jid)
        finally:
            await engine.stop()
        assert job["state"]["partial_failure"] and not job["state"]["list_complete"]
        assert (await db.task_counts(jid))["failed"] == 1
        assert not await db._one("SELECT 1 FROM source_checks LIMIT 1")
    asyncio.run(run())


def test_cancel_running_prefix_finishes_batch(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        jid = await create(engine, body(scope_id))
        entered, released = asyncio.Event(), asyncio.Event()
        class SlowSite:
            async def get_page(self, path):
                entered.set()
                await released.wait()
                from hls2strm.fetcher import Page
                return Page(listing("abc-001"), "https://missav" + path, "https://missav")
        fetcher.site = lambda _: SlowSite()
        await engine.start()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await cancel(engine, jid, "missav", "ABC")
            released.set()
            assert (await wait_job(db, jid))["status"] == "done"
        finally:
            released.set()
            await engine.stop()
        assert not await db.find_source("missav", "abc-001")
    asyncio.run(run())


def test_restart_preserves_snapshot_plan_and_results(make_store, boot):
    async def run():
        db, engine, fetcher, scope_id = await setup(make_store, boot)
        jid = await create(engine, body(scope_id))
        await engine.set_job_status(jid, "pause")
        await db.close()
        db = Database(boot.data_dir / "test.db")
        await db.open()
        try:
            store = SettingsStore(boot, db)
            await store.load()
            engine = build(db, store, boot, fetcher)
            fetcher.pages["missav"]["/cn/search/ABC?page=1"] = listing("abc-001")
            assert (await statistics(db, scope_id, "missav"))["total"] == 1
            assert (await progress(db, jid))["items"][0]["prefix"] == "ABC"
            await engine.start()
            try:
                await engine.set_job_status(jid, "resume")
                await wait_job(db, jid)
            finally:
                await engine.stop()
            await db.delete_job(jid)
            assert not (await progress(db, jid))["items"]
            assert await db.get_video("abc-001")
        finally:
            await db.close()
    asyncio.run(run())


def test_prefix_api_auth_validation_and_idempotency(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path, ui_password="test"))
    with TestClient(app) as client:
        assert client.post("/api/prefixes/stats", json={}).status_code == 401
        assert client.post("/api/prefixes/jobs", json={}).status_code == 401
        assert client.post("/api/prefixes/jobs/1/cancel", json={}).status_code == 401
        auth = ("admin", "test")
        stat = client.post("/api/prefixes/stats", auth=auth, json={}).json()
        headers = {"Idempotency-Key": "prefix-api-test"}
        value = body(stat["scope_id"])
        value["prefixes"] = ["abc"]
        first = client.post("/api/prefixes/jobs", auth=auth, headers=headers, json=value)
        assert first.status_code == 200, first.text
        repeated = client.post("/api/prefixes/jobs", auth=auth, headers=headers, json=value)
        assert repeated.json() == first.json()
        jid = first.json()["job_id"]
        assert client.get(f"/api/prefixes/jobs/{jid}/summary", auth=auth).json()["sequence"]["total"] == 1
        assert client.get(f"/api/prefixes/jobs/{jid}/progress?state=queued", auth=auth).status_code == 200
        assert client.get(f"/api/prefixes/jobs/{jid}/progress?state=invalid", auth=auth).status_code == 422
        assert client.post("/api/prefixes/jobs", auth=auth, json={**value, "max_pages": -1}).status_code == 422
        assert client.post("/api/prefixes/jobs", auth=auth, json={**value, "prefixes": ["../oops"]}).status_code == 400
        assert client.post("/api/prefixes/stats", auth=auth, json={"query": {"unknown": "foo"}}).status_code == 400


@pytest.mark.parametrize("name", list(SITES))
def test_site_prefix_search_is_valid_list_source(name):
    site = SITES[name]
    path = site.prefix_source("JUFE")
    assert "JUFE" in path and site.page_url(path, 2)
