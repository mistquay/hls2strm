import asyncio
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from hls2strm.app import create_app
from hls2strm.config import BootConfig, SettingsStore
from hls2strm.db import Database
from hls2strm.errors import FetchError
from hls2strm.play_history import PlaybackHistory
from hls2strm.sites import SourceDetail
from .conftest import FakeFetcher


@contextmanager
def history_client(tmp_path,password=""):
    app=create_app(BootConfig(data_dir=tmp_path,ui_password=password))
    with TestClient(app) as client:
        ctx=app.state.ctx
        ctx.engine.pause()
        fake=FakeFetcher("",{})
        ctx.fetcher=ctx.resolver.fetcher=ctx.engine.fetcher=fake
        ctx.resolver.quality.fetcher=fake
        async def seed():
            await ctx.store.update({"quality_capture":False,"download_cover":False,"health_interval":0,
                                    "resolve_token":"resolver-secret","play_token":"player-secret"})
            for site in ("jable","missav"):
                await ctx.db.upsert_detail(site,SourceDetail(key="abc-001",code="ABC-001",title="播放验收影片",
                    duration=100,stream_url=f"https://{site}.invalid/main.m3u8?signature=cdn-secret",stream_expires=int(time.time())+14400),
                    "abc-001",ctx.store.current.site_rank)
            async def preflight(resolved,want):
                pass
            ctx.resolver.selection.probe=preflight
        client.portal.call(seed)
        # FakeFetcher 的清单响应不依赖带签名的真实地址。
        async def playlist(url,headers=None):
            return b"#EXTM3U\n#EXTINF:100,\na.ts\n#EXT-X-ENDLIST\n"
        fake.get_playlist=playlist
        yield client,ctx,fake


def flush(client,ctx):
    client.portal.call(ctx.play_history.queue.join)


def test_actual_routes_filters_and_probes_do_not_count(tmp_path):
    with history_client(tmp_path) as (client,ctx,fake):
        original_get=fake.session.get
        async def segment(url,**kw):
            response=await original_get(url,**kw)
            response.status_code=200
            return response
        fake.session.get=segment
        assert client.head("/play/abc-001.m3u8?t=player-secret",follow_redirects=False).status_code==302
        prepared=client.get("/play/abc-001.m3u8?prepare=1&t=player-secret&src=missav").json()
        assert client.get("/play/abc-001.m3u8?prepare=yes&t=player-secret&src=missav").status_code==200
        flush(client,ctx)
        assert client.get("/api/play-history").json()["total"]==0
        assert client.get("/play/abc-001.m3u8?t=player-secret&src=jable",follow_redirects=False).status_code==302
        assert client.get(prepared["url"].replace("../","/",1)).status_code==200
        proxy=client.get("/play/abc-001.m3u8?t=player-secret&proxy=1&src=jable")
        assert proxy.status_code==200
        src=prepared["source_id"]
        assert client.get(f"/hls/{src}/a.ts?t=player-secret").status_code==200
        flush(client,ctx)
        page=client.get("/api/play-history").json()
        assert page["total"]==3 and page["counts"]["redirect"]==1 and page["counts"]["proxy"]==2
        direct=client.get("/api/play-history?mode=redirect&site=jable&q=ABC-001").json()
        assert direct["total"]==1 and direct["items"][0]["state"]=="delivered"
        proxied=client.get("/api/play-history?mode=proxy&site=missav").json()
        assert proxied["total"]==1 and proxied["items"][0]["source_id"]==prepared["source_id"]
        serialized=str(page)
        assert "cdn-secret" not in serialized and "player-secret" not in serialized and "session_key" not in serialized
        assert client.get("/api/play-history?since=200&until=100").status_code==422
        assert client.get("/api/play-history?site=unknown").status_code==422


def test_gateway_resolution_and_actual_relay_share_one_record(tmp_path):
    with history_client(tmp_path) as (client,ctx,fake):
        header={"Authorization":"Bearer resolver-secret"}
        bound=client.get("/api/resolve/abc-001?relay=gateway&mode=auto",headers=header).json()
        # 首先指定站点优先，使实际源为需要中转的 MissAV。
        assert not bound["relay"]
        assert client.put("/api/settings",json={"site_priority":["missav","jable"]}).status_code==200
        bound=client.get("/api/resolve/abc-001?relay=gateway&mode=auto&ua=OriginalPlayer",headers=header).json()
        assert bound["relay"] and bound["source"]["site"]=="missav"
        flush(client,ctx)
        assert client.get("/api/play-history?mode=proxy&state=issued").json()["total"]==1
        url=bound["url"]
        parsed=urlsplit(url)
        assert client.get(parsed.path+"?"+parsed.query).status_code==200
        flush(client,ctx)
        rows=client.get("/api/play-history?mode=proxy").json()
        assert rows["total"]==1 and rows["items"][0]["state"]=="delivered"
        assert rows["items"][0]["entry"]=="gateway" and rows["items"][0]["user_agent"]=="OriginalPlayer"
        # 强制中转时先返回未选源的播放地址，再由真正的播放补齐站点。
        fallback=client.get("/api/resolve/abc-001?relay=gateway&mode=proxy",headers=header).json()
        parsed=urlsplit(fallback["url"])
        assert parse_qs(parsed.query)["ph"]
        assert client.get(parsed.path+"?"+parsed.query).status_code==200
        flush(client,ctx)
        rows=client.get("/api/play-history?mode=proxy").json()
        assert rows["total"]==2 and all(row["site"]=="missav" and row["state"]=="delivered" for row in rows["items"])


def test_range_requests_and_restart_do_not_duplicate_session(tmp_path):
    session_url=""
    with history_client(tmp_path) as (client,ctx,fake):
        async def change_to_file():
            source=next(s for s in await ctx.db.get_sources(1) if s["site"]=="jable")
            await ctx.db._write("UPDATE sources SET stream_url='https://cdn.invalid/file.mp4' WHERE id=?",(source["id"],))
        client.portal.call(change_to_file)
        prepared=client.get("/play/abc-001.m3u8?prepare=1&src=jable&t=player-secret").json()
        session_url=prepared["url"].replace("../","/",1)
        for _ in range(8):
            assert client.get(session_url,headers={"Range":"bytes=0-1023"}).status_code==206
        flush(client,ctx)
        assert client.get("/api/play-history").json()["total"]==1
    app=create_app(BootConfig(data_dir=tmp_path))
    with TestClient(app) as client:
        ctx=app.state.ctx
        fake=FakeFetcher("",{})
        ctx.fetcher=ctx.resolver.fetcher=fake
        assert client.get(session_url,headers={"Range":"bytes=10-20"}).status_code==206
        flush(client,ctx)
        assert client.get("/api/play-history").json()["total"]==1


def test_history_auth_failure_and_disable(tmp_path):
    with history_client(tmp_path,password="admin-secret") as (client,ctx,fake):
        assert client.get("/api/play-history").status_code==401
        assert client.get("/play/abc-001.m3u8?t=bad",follow_redirects=False).status_code==403
        async def broken(resolved,want):
            raise FetchError("CDN failed")
        ctx.resolver.selection.probe=broken
        assert client.get("/play/abc-001.m3u8?t=player-secret").status_code==502
        flush(client,ctx)
        client.post("/api/login",json={"username":"admin","password":"admin-secret"})
        rows=client.get("/api/play-history?state=failed").json()
        assert rows["total"]==1 and rows["items"][0]["mode"]==""
        assert client.put("/api/settings",json={"play_history_enabled":False}).status_code==200
        client.get("/play/abc-001.m3u8?t=player-secret")
        flush(client,ctx)
        assert client.get("/api/play-history").json()["total"]==1


def test_queue_keeps_playback_nonblocking_and_retention_survives_restart(make_store,boot):
    async def run():
        db,store=await make_store()
        await store.update({"play_history_pending":2,"play_history_max_entries":2,"play_history_days":1})
        history=PlaybackHistory(db,store)
        now=int(time.time())
        def record(key,stamp):
            return {"session_key":key,"slug":key,"created_at":stamp,"updated_at":stamp,"entry":"play","mode":"redirect","state":"delivered","http_status":302}
        history.submit(record("old",now-2*86400))
        history.submit(record("first",now))
        history.submit(record("dropped",now))
        assert history.dropped==1
        history.start()
        await history.queue.join()
        history.submit(record("second",now+1))
        await history.queue.join()
        history.submit(record("third",now+2))
        await history.queue.join()
        await history.cleanup()
        assert (await history.page())["total"]==2
        assert [r["slug"] for r in (await history.page())["items"]]==["third","second"]
        await history.stop()
    asyncio.run(run())
