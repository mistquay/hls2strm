import asyncio
import time
from urllib.parse import urlsplit

import httpx

from hls2strm.errors import FetchError, PoolBusy
from hls2strm.media_format import media_container
from .test_startup_recovery import Media, setup


def test_prepare_advertises_actual_mp4_and_restores_the_bound_snapshot(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver", "play_token": "play-token"})
        fetcher.file_session = Media()
        fetcher.file_session.fail_site = "unused"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            path = "/api/resolve/abc-001.m3u8?prepare=1&relay=gateway&mode=proxy"
            assert (await client.get(path)).status_code == 401
            response = await client.get(path, headers={"Authorization": "Bearer resolver"})
            assert response.status_code == 200
            prepared = response.json()
            assert prepared["protocol_version"] == 1 and prepared["transcoding"] is False
            assert (prepared["container"], prepared["media_type"], prepared["relay"]) == ("mp4", "file", True)
            assert urlsplit(prepared["url"]).path.endswith(".mp4")
            assert "t=play-token" in prepared["url"]
            source_id = prepared["source"]["source_id"]
            await db.set_stream(source_id, "https://changed.invalid/movie.mp4", int(time.time())+20000)
            resolver.sessions.items.clear()
            head = await client.head(prepared["url"])
            assert head.status_code == 200 and head.headers["content-type"] == "video/mp4" and not head.content
            wrong = prepared["url"].replace(".mp4?", ".m3u8?")
            assert (await client.get(wrong)).status_code == 409
            assert (await client.get(prepared["url"].replace("play-token", "wrong"))).status_code == 403
            response = await client.get(prepared["url"], headers={"Range": "bytes=0-"})
            assert response.status_code == 200 and response.content == b"data"
            assert fetcher.file_session.calls == ["https://jable.invalid/movie.mp4"]*2
    asyncio.run(run())


def test_legacy_hls_request_skips_mp4_without_penalizing_it(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        video = await db.get_video("abc-001")
        sources = await db.get_sources(video["id"])
        hls_source = next(s for s in sources if s["site"] == "missav")
        await db.set_stream(hls_source["id"], "https://missav.invalid/movie.m3u8", int(time.time())+20000)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&container=m3u8",
                                        headers={"Authorization": "Bearer resolver"})
            assert response.status_code == 200
            prepared = response.json()
            assert prepared["container"] == "m3u8" and prepared["source"]["site"] == "missav"
            response = await client.get(prepared["url"])
            assert response.status_code == 200 and response.text.startswith("#EXTM3U")
            for source in sources:
                assert (await db.get_source(source["id"]))["fail_streak"] == 0
            assert not resolver.selection.cooling
            response = await client.get("/api/resolve/abc-001?prepare=1&container=exe",
                                        headers={"Authorization": "Bearer resolver"})
            assert response.status_code == 422
    asyncio.run(run())


def test_prepared_mp4_does_not_recover_into_hls(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        video = await db.get_video("abc-001")
        hls_source = next(s for s in await db.get_sources(video["id"]) if s["site"] == "missav")
        await db.set_stream(hls_source["id"], "https://missav.invalid/movie.m3u8", int(time.time())+20000)
        fetcher.file_session = Media()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            prepared = (await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=proxy",
                                         headers={"Authorization": "Bearer resolver"})).json()
            response = await client.get(prepared["url"])
            assert response.status_code == 502
            assert "location" not in response.headers
            assert fetcher.file_session.calls == ["https://jable.invalid/movie.mp4"]
            assert (await db.get_source(hls_source["id"]))["fail_streak"] == 0
    asyncio.run(run())


def test_prepared_startup_recovers_same_format_with_gateway_prefix(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        fetcher.file_session = Media()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            prepared = (await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=proxy",
                                         headers={"Authorization": "Bearer resolver"})).json()
            prefix = "/stream/http_resolver/ticket"
            headers = {"X-Forwarded-Prefix": prefix}
            response = await client.get(prepared["url"], headers=headers)
            assert response.status_code == 307 and response.headers["location"].startswith(prefix+"/media/")
            replacement = response.headers["location"].removeprefix(prefix)
            final = await client.get(replacement, headers=headers)
            assert final.status_code == 200 and final.headers["content-type"] == "video/mp4"
            resolver.sessions.items.clear()
            repeated = await client.get(prepared["url"], headers=headers)
            assert repeated.status_code == 307 and repeated.headers["location"] == response.headers["location"]
    asyncio.run(run())


def test_prepare_failures_never_return_an_unresolved_play_url(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for failure, status in [(FetchError("bad CDN"), 502), (PoolBusy("local pool full"), 503)]:
                async def probe(*args):
                    raise failure
                monkeypatch.setattr(resolver.selection, "probe", probe)
                response = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=proxy",
                                            headers={"Authorization": "Bearer resolver"})
                assert response.status_code == status and "url" not in response.json()
    asyncio.run(run())


def test_prepare_preserves_direct_delivery_for_compatible_clients(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&ua=Infuse",
                                        headers={"Authorization": "Bearer resolver"})
            prepared = response.json()
            assert response.status_code == 200 and prepared["relay"] is False
            assert prepared["container"] == "mp4" and prepared["url"] == "https://jable.invalid/movie.mp4"
    asyncio.run(run())


def test_container_detection_handles_extensionless_media_and_real_headers():
    assert media_container("https://cdn.invalid/stream", "video/mp4") == "mp4"
    assert media_container("https://cdn.invalid/file.bin", "application/octet-stream", b"\0\0\0\x20ftypisom") == "mp4"
    assert media_container("https://cdn.invalid/stream", "video/webm") == "webm"
    assert media_container("https://cdn.invalid/stream", "video/x-matroska") == "mkv"
    assert media_container("https://cdn.invalid/stream", "application/vnd.apple.mpegurl") == "m3u8"
    assert media_container("https://cdn.invalid/stream", "application/octet-stream") == ""


def test_prepare_detects_extensionless_mp4_through_real_preflight(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        monkeypatch.delattr(resolver.selection, "probe")
        video = await db.get_video("abc-001")
        source = next(s for s in await db.get_sources(video["id"]) if s["site"] == "jable")
        original = "https://cdn.invalid/stream?signature=original"
        await db.set_stream(source["id"], original, int(time.time())+20000)
        class NativeMedia:
            async def get(self, url, **kwargs):
                class Response:
                    status_code = 206
                    headers = {"content-type": "video/mp4"}
                    async def aiter_content(self):
                        yield b"\0\0\0\x20ftypisom" + b"\0"*32768
                    async def aclose(self):
                        pass
                return Response()
        fetcher.preflight_session = fetcher.file_session = NativeMedia()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=proxy",
                                        headers={"Authorization": "Bearer resolver"})
            assert response.status_code == 200
            prepared = response.json()
            assert prepared["container"] == "mp4" and ".mp4?" in prepared["url"]
            response = await client.get(prepared["url"], headers={"Range": "bytes=0-"})
            assert response.status_code == 206 and response.headers["content-type"] == "video/mp4"
            assert response.content[4:8] == b"ftyp"
    asyncio.run(run())


def test_prepare_respects_strict_direct_and_redirect_fallback(make_store, monkeypatch):
    async def run():
        db, store, resolver, fetcher, app = await setup(make_store, monkeypatch)
        await store.update({"resolve_token": "resolver"})
        await db._write("DELETE FROM sources WHERE site='jable'")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer resolver"}
            strict = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=strict_redirect", headers=headers)
            assert strict.status_code == 409
            fallback = await client.get("/api/resolve/abc-001?prepare=1&relay=gateway&mode=redirect", headers=headers)
            assert fallback.status_code == 200 and fallback.json()["relay"] is True
            unavailable = await client.get("/api/resolve/abc-001?prepare=1&mode=proxy", headers=headers)
            assert unavailable.status_code == 409
    asyncio.run(run())
