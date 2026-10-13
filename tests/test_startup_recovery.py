import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urljoin

import httpx
import pytest
from fastapi import FastAPI

from hls2strm.errors import FetchError, PoolBusy
from hls2strm.observability import Metrics
from hls2strm.play import Resolver, router
from hls2strm.runtime import RequestTelemetry
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher


async def setup(make_store, monkeypatch):
    db, store = await make_store()
    await store.update({"quality_capture": False, "play_discover": False})
    for site in ("jable", "missav"):
        await db.upsert_detail(site, SourceDetail(key="abc-001", code="ABC-001", title="ABC",
            stream_url=f"https://{site}.invalid/movie.mp4", stream_expires=int(time.time()) + 20000),
            "abc-001", store.current.site_rank)
    fetcher = FakeFetcher("", {})
    resolver = Resolver(db, fetcher, store, Metrics())
    async def probe(*args):
        pass
    monkeypatch.setattr(resolver.selection, "probe", probe)
    app = FastAPI()
    app.include_router(router)
    app.state.ctx = SimpleNamespace(db=db, store=store, resolver=resolver, fetcher=fetcher, metrics=resolver.metrics)
    app.add_middleware(RequestTelemetry)
    return db, store, resolver, fetcher, app


class Media:
    def __init__(self, failure="open"):
        self.failure, self.fail_site = failure, "jable"
        self.calls, self.closed = [], []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        failing = self.fail_site in url
        if failing and self.failure == "open":
            raise OSError("connection reset")
        if failing and self.failure == "busy":
            raise PoolBusy("local pool full")
        owner = self
        class Response:
            status_code = 502 if failing and owner.failure == "status" else 200
            headers = {"content-type": "video/mp4", "content-length": "4"}
            async def aiter_content(self):
                if failing and owner.failure == "first":
                    raise OSError("first chunk failed")
                yield b"data"
            async def aclose(self):
                owner.closed.append(url)
        return Response()


@pytest.mark.parametrize("failure", ["open", "status", "first"])
def test_play_recovers_before_headers(make_store, monkeypatch, failure):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        media = fetcher.file_session = Media(failure)
        # 其他影片积累的冷却记录不能塞满本次播放的恢复排除上限。
        for i in range(1000, 1100):
            resolver.selection.cooling[(i, 0)] = time.monotonic() + 300
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            r = await client.get("/play/abc-001@720p.m3u8?proxy=1")
            assert r.status_code == 200 and r.content == b"data"
            assert [u.split(".")[0] for u in media.calls] == ["https://jable", "https://missav"]
            assert any("missav" in u for u in media.closed)
            assert resolver.selection.cooling
    asyncio.run(run())


@pytest.mark.parametrize("query,headers,failure,status", [
    ("&src=jable", {}, "open", 502),
    ("", {"Range": "bytes=100-"}, "open", 502),
    ("", {}, "busy", 503),
])
def test_explicit_resume_and_pool_busy_do_not_switch(make_store, monkeypatch, query, headers, failure, status):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        media = fetcher.file_session = Media(failure)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            r = await client.get("/play/abc-001.m3u8?proxy=1" + query, headers=headers)
            assert r.status_code == status
            assert len(media.calls) == 1
            assert not resolver.selection.cooling
    asyncio.run(run())


def test_new_session_redirects_but_started_session_stays_pinned_after_restart(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        media = fetcher.file_session = Media()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            prepared = await client.get("/play/abc-001@720p.m3u8?prepare=1&variants=all")
            first_url = urljoin(str(prepared.url), prepared.json()["url"])
            first = await client.get(first_url)
            assert first.status_code == 307
            next_url = urljoin(first_url, first.headers["location"])
            assert "want=720" in next_url and "variants=all" in next_url
            final = await client.get(next_url)
            assert final.status_code == 200 and final.content == b"data"
            resolver.sessions.items.clear()  # 模拟重启，从数据库恢复已交付标记。
            media.fail_site = "missav"
            failed = await client.get(next_url, headers={"Range": "bytes=0-"})
            assert failed.status_code == 502 and "location" not in failed.headers
            media.fail_site = "jable"
            repeated = await client.get(first_url)
            assert repeated.status_code == 307 and repeated.headers["location"] == first.headers["location"]
    asyncio.run(run())


@pytest.mark.parametrize("discovered_works", [True, False])
def test_preflight_exclusions_allow_one_discovery_and_keep_quality(make_store, monkeypatch, discovered_works):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"play_discover": True, "sites": {name: {"enabled": name in ("jable", "missav")}
                                                           for name in store.current.site_priority}})
        await db._write("DELETE FROM sources WHERE site='missav'")
        calls, wants = [], []
        async def find(site, *args, **kwargs):
            calls.append(site.name)
            return [SourceDetail(key="abc-001", code="ABC-001", title="ABC",
                                 stream_url="https://missav.invalid/movie.mp4", stream_expires=int(time.time()) + 20000)]
        async def probe(r, want):
            wants.append(want)
            if r.site.name == "jable" or not discovered_works:
                raise FetchError("bad media")
        monkeypatch.setattr("hls2strm.play.find_by_code", find)
        monkeypatch.setattr(resolver.selection, "probe", probe)
        if discovered_works:
            assert (await resolver.selection.choose("abc-001", want=720)).site.name == "missav"
        else:
            with pytest.raises(FetchError):
                await resolver.selection.choose("abc-001", want=720)
        assert calls == ["missav"] and all(w == 720 for w in wants)
    asyncio.run(run())


def test_failure_repair_uses_queue_once_and_respects_negative_checks(make_store, boot):
    async def run():
        from .test_engine import build
        db, store = await make_store()
        vid = await db.upsert_detail("jable", SourceDetail(key="abc-001", code="ABC-001", title="ABC"),
                                     "abc-001", store.current.site_rank)
        await store.update({"sites": {name: {"enabled": name in ("jable", "missav", "123av")}
                                      for name in store.current.site_priority}})
        await db.set_source_check(vid, "123av", False)
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine._queue_play_probe("abc-001")
        await engine._queue_play_probe("abc-001")
        rows = await db._all("SELECT * FROM tasks WHERE kind='probe'")
        assert len(rows) == 1 and rows[0]["target"] == f"missav:{vid}"
    asyncio.run(run())
