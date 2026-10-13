import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig
from hls2strm.errors import FetchError, NotFound
from hls2strm.observability import Metrics
from hls2strm.play import Resolved, Resolver, playback_source
from hls2strm.sites import SITES, SourceDetail
from .conftest import FakeFetcher


async def seed(db, store, *, mode="both", site="jable", subtitle="", own_subtitle=""):
    await store.update({"edition_fallback": mode, "play_discover": False, "quality_capture": False,
                        "download_cover": False})
    ids = []
    for u in (False, True):
        slug = "luxu-1850" + ("-u" if u else "")
        vid = await db.upsert_detail(site if u else "jable", SourceDetail(key=slug, code="LUXU-1850", title=slug,
                                     uncensored=u, duration=1000 if u else 500,
                                     subtitle=subtitle if u else own_subtitle,
                                     stream_url=f"https://cdn.invalid/{slug}.m3u8", stream_expires=int(time.time()) + 14400),
                                     slug, store.current.site_rank)
        ids.append(vid)
    return ids


@pytest.mark.parametrize("mode,u,allowed", [
    ("off", False, False), ("off", True, False), ("both", False, True), ("both", True, True),
    ("normal_uses_u", False, True), ("normal_uses_u", True, False),
    ("u_uses_normal", False, False), ("u_uses_normal", True, True),
])
def test_directions_only_after_own_failure(make_store, monkeypatch, mode, u, allowed):
    async def run():
        db, store = await make_store()
        ids = await seed(db, store, mode=mode)
        requested, served = ids[int(u)], ids[int(not u)]
        v = await db.get_video_by_id(requested)
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        calls = []
        async def ensure(owner, source, need, **options):
            calls.append((owner["id"], need, options["height"]))
            assert owner["id"] == source["video_id"]
            if owner["id"] == requested:
                raise FetchError("模拟本版源失败")
            return Resolved(owner, source, SITES[source["site"]])
        monkeypatch.setattr(resolver, "_ensure", ensure)
        if allowed:
            r = await resolver.resolve(v["slug"], want=720)
            assert r.requested["id"] == requested and r.video["id"] == served
            assert playback_source(r)["edition_fallback"]
            assert playback_source(r)["served_slug"] == r.video["slug"]
            assert calls == [(requested, resolver._need(v, None), 720),
                             (served, resolver._need(r.video, None), 720)]
            assert resolver.metrics.counters["play_edition_fallback"] == 1
        else:
            with pytest.raises(FetchError):
                await resolver.resolve(v["slug"], want=720)
            assert [call[0] for call in calls] == [requested]
        assert (await db.get_sources(served))[0]["fail_streak"] == 0
        assert (await db.get_sources(requested))[0]["fail_streak"] == 1
    asyncio.run(run())


def test_own_success_and_preflight_failure(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store)
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        good = await resolver.resolve("luxu-1850")
        assert good.video["id"] == normal and good.requested is None
        assert not resolver.metrics.counters["play_edition_fallback"]
        seen = []
        async def probe(r, want):
            seen.append((r.video["id"], want))
            if r.video["id"] == normal:
                raise FetchError("模拟本版媒体预检失败")
        monkeypatch.setattr(resolver.selection, "probe", probe)
        r = await resolver.selection.choose("luxu-1850", want=720)
        assert seen == [(normal, 720), (u, 720)] and r.requested["id"] == normal
        assert (await db.get_sources(normal))[0]["fail_streak"] == 1
        assert (await db.get_sources(u))[0]["fail_streak"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("restriction", ["site", "direct", "subtitle", "excluded", "edition_only"])
def test_constraints_cannot_be_bypassed(make_store, monkeypatch, restriction):
    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store, site="missav" if restriction == "direct" else "jable",
                               own_subtitle="zh", subtitle="en" if restriction == "subtitle" else "zh")
        if restriction == "subtitle":
            await store.update({"subtitle_fallback": False})
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        own, other = (await db.get_sources(normal))[0], (await db.get_sources(u))[0]
        seen = []
        async def ensure(v, src, need, **kw):
            seen.append(v["id"])
            if v["id"] == normal:
                raise FetchError("本版失败")
            return Resolved(v, src, SITES[src["site"]])
        monkeypatch.setattr(resolver, "_ensure", ensure)
        kw = {"site": "jable"} if restriction == "site" else {"direct_only": True, "remote": True} if restriction == "direct" else {"edition_only": True} if restriction == "edition_only" else {}
        if restriction in ("subtitle", "excluded"):
            # 排除本版不允许字幕档随之降级；明确排除的兄弟源也不能回来。
            kw["excluded"] = frozenset({(own["id"], 0)} | ({(other["id"], 0)} if restriction == "excluded" else set()))
        with pytest.raises((NotFound, FetchError)):
            await resolver.resolve("luxu-1850", **kw)
        assert u not in seen
    asyncio.run(run())


def test_no_own_sources_uses_siblings_best_subtitle(make_store):
    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store, subtitle="en", own_subtitle="zh")
        await store.update({"subtitle_fallback": False})
        await db._write("UPDATE sources SET status='removed' WHERE video_id=?", (normal,))
        r = await Resolver(db, FakeFetcher("", {}), store, Metrics()).resolve("luxu-1850")
        assert r.video["id"] == u and r.source["subtitle"] == "en"
        assert (await db.get_sources(normal))[0]["status"] == "removed"
    asyncio.run(run())


def test_discovery_only_requested_edition_and_no_excluded(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        normal, _ = await seed(db, store)
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        discovered = []
        async def ensure(*args, **kw):
            raise FetchError("所有已知源都失败")
        async def discover(v, deadline, **kw):
            discovered.append(v["id"])
            return None
        monkeypatch.setattr(resolver, "_ensure", ensure)
        monkeypatch.setattr(resolver, "_discover_for", discover)
        with pytest.raises(FetchError):
            await resolver.resolve("luxu-1850")
        assert discovered == [normal]
        with pytest.raises(FetchError):
            await resolver.resolve("luxu-1850", excluded=frozenset({(999, 0)}))
        assert discovered == [normal]
    asyncio.run(run())


def test_multiple_siblings_status_and_native_boundary(make_store):
    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store)
        v = await db.get_video_by_id(normal)
        await db._write("INSERT INTO videos(slug,code,code_key,uncensored,status,created_at,updated_at) VALUES('dup','LUXU-1850','LUXU-1850',1,'gone',1,1)")
        assert [s["id"] for s in await db.sibling_videos(v)] == [u]
        assert len(await db.sibling_videos(v, active_only=False)) == 2
        await db._write("UPDATE videos SET status='active' WHERE slug='dup'")
        assert len(await db.sibling_videos(v)) == 2
        assert not await db.sibling_videos({**v, "code_key": ""})
        assert not await db.sibling_videos({**v, "code_key": "1PON-100826_001"})
    asyncio.run(run())


def test_session_keeps_requested_and_served_after_restart_and_refresh(make_store, monkeypatch):
    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store)
        await db._write("UPDATE sources SET status='gone' WHERE video_id=?", (normal,))
        resolver = Resolver(db, FakeFetcher("", {}), store, Metrics())
        r = await resolver.resolve("luxu-1850")
        sid = await resolver.sessions.persist(db, r)
        await store.update({"edition_fallback": "off"})
        fresh = Resolver(db, FakeFetcher("", {}), store, Metrics())
        restored = await fresh.session_source(r.source["id"], sid)
        assert restored.requested["id"] == normal and restored.video["id"] == u
        async def refresh(source_id, **kw):
            assert source_id == r.source["id"]
            return Resolved(r.video, {**r.source, "stream_url": "https://cdn.invalid/fresh.m3u8"}, r.site)
        monkeypatch.setattr(fresh, "ensure_source", refresh)
        restored = await fresh.session_source(r.source["id"], sid, stale=r.url)
        assert restored.requested["id"] == normal and restored.video["id"] == u
    asyncio.run(run())


def test_prepare_history_and_sibling_details(tmp_path):
    app = create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.engine.pause()
        async def prepare():
            ids = await seed(ctx.db, ctx.store)
            await ctx.db._write("UPDATE sources SET status='gone' WHERE video_id=?", (ids[0],))
            await ctx.db._write("INSERT INTO video_aliases VALUES('old-normal',?,1,NULL)", (ids[0],))
            return ids
        normal, u = client.portal.call(prepare)
        fake = FakeFetcher("", {})
        ctx.fetcher = ctx.resolver.fetcher = ctx.engine.fetcher = fake
        async def probe(r, want):
            pass
        ctx.resolver.selection.probe = probe
        r = client.get("/play/old-normal@720p.m3u8?prepare=1")
        assert r.status_code == 200
        prepared = r.json()
        assert prepared["served_slug"] == "luxu-1850-u" and prepared["edition_fallback"]
        assert "want=720" in prepared["url"]
        detail = client.get("/api/videos/old-normal").json()
        assert detail["siblings"] == [{"slug": "luxu-1850-u", "uncensored": 1, "status": "active"}]
        # prepare 不记播放；真正交付后按请求影片记历史，并记录实际供源影片。
        assert client.get("/api/play-history").json()["total"] == 0
        r = client.get("/play/old-normal.m3u8", follow_redirects=False)
        assert r.status_code == 302
        client.portal.call(ctx.play_history.queue.join)
        row = client.get("/api/play-history").json()["items"][0]
        assert row["video_id"] == normal and row["slug"] == "luxu-1850"
        assert row["served_video_id"] == u and row["served_slug"] == "luxu-1850-u"
        # 网页明确指定 src，以及已起播恢复的 edition_only，都不能跨版本。
        assert client.get("/play/luxu-1850.m3u8?prepare=1&src=jable").status_code == 404
        assert client.get("/play/luxu-1850.m3u8?prepare=1&edition_only=1").status_code == 404


@pytest.mark.parametrize("served_u", [True, False])
def test_proxy_playlist_reresolves_requested_video_and_keeps_identity(make_store, monkeypatch, served_u):
    import importlib
    import json
    from types import SimpleNamespace
    from starlette.requests import Request

    play_module = importlib.import_module("hls2strm.play")

    async def run():
        db, store = await make_store()
        normal, u = await seed(db, store)
        await db._write("UPDATE sources SET status='gone' WHERE video_id=?", (normal,))
        fake = FakeFetcher("", {})
        resolver = Resolver(db, fake, store, Metrics())
        r = await resolver.resolve("luxu-1850")
        r.source = {**r.source, "stream_expires": int(time.time()) + 90}
        ctx = SimpleNamespace(db=db, store=store, resolver=resolver, metrics=resolver.metrics, fetcher=fake)
        request = Request({"type": "http", "method": "GET", "path": "/play/luxu-1850@720p.m3u8",
                           "query_string": b"", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(ctx=ctx))})
        reads = []
        async def playlist(url, headers=None):
            reads.append(url)
            return b"#EXTM3U\n#EXTINF:100,\nhttps://cdn.invalid/segment.ts?signature=x\n"
        fake.get_playlist = playlist
        calls = []
        async def resolve(req, slug, **kw):
            calls.append((slug, kw))
            vid = u if served_u else normal
            video = await db.get_video_by_id(vid)
            source = (await db.get_sources(vid))[0]
            return Resolved(video, {**source, "stream_url": "https://cdn.invalid/refreshed.m3u8"}, SITES["jable"])
        monkeypatch.setattr(play_module, "_resolve_or_http", resolve)
        response = await play_module._proxy_playlist(request, r, "", want=720)
        assert response.status_code == 200 and len(reads) == 2
        assert calls[0][0] == "luxu-1850" and calls[0][1]["want"] == 720
        assert calls[0][1]["min_remaining"] == resolver._need(r.video, None)
        history = request.scope["play_history"]
        assert history["video_id"] == normal and history["slug"] == "luxu-1850"
        assert history["served_video_id"] == (u if served_u else normal)
        session = json.loads((await db._one("SELECT data FROM play_sessions"))["data"])
        assert (session["requested"]["id"] if session["requested"] else None) == (normal if served_u else None)
    asyncio.run(run())
