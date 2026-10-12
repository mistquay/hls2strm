import asyncio
from copy import copy
from dataclasses import replace
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from hls2strm.cache import AsyncCache
from hls2strm.config import Settings
from hls2strm.http_resources import ManagedSession
from hls2strm.observability import Metrics
from hls2strm.play import Resolved, router
from hls2strm.sites import get_site


class Response:
    def __init__(self, status, headers, body=b""):
        self.status_code, self.headers, self.body = status, headers, body
        self.closed = self.consumed = False

    async def aiter_content(self):
        self.consumed = True
        yield self.body

    async def aclose(self):
        self.closed = True


def relay_app(responses, site="missav", fake_header=False):
    calls, health = [], []
    adapter = copy(get_site(site))
    adapter.stream = replace(adapter.stream, fake_header=fake_header)
    resolved = Resolved({"slug": "abc-001"}, {"id": 1, "key": "abc-001",
                        "stream_url": "https://cdn.invalid/master.m3u8"}, adapter)

    async def source(*args, **kw):
        return resolved

    async def request(method, url, **kw):
        response = responses.pop(0)
        calls.append((method, url, kw, response))
        return response

    async def get(url, **kw):
        return await request("GET", url, **kw)

    session = SimpleNamespace(request=request, get=get)
    fetcher = SimpleNamespace(session=session, hls_session=session, playlists=AsyncCache())
    app = FastAPI()
    app.include_router(router)
    app.state.ctx = SimpleNamespace(store=SimpleNamespace(current=Settings()), fetcher=fetcher, metrics=Metrics(),
                                    resolver=SimpleNamespace(session_source=source,
                                        health=SimpleNamespace(record=lambda *args, **kw: health.append((args, kw)))))
    return app, calls, health


def test_segment_head_does_not_consume_body_and_range_preserves_partial_response():
    async def run():
        probe = Response(200, {"content-length": "8", "content-type": "image/jpeg"})
        partial = Response(206, {"content-length": "4", "content-range": "bytes 4-7/8",
                                "accept-ranges": "bytes"}, b"data")
        invalid = Response(416, {"content-range": "bytes */8"})
        app, calls, health = relay_app([probe, partial, invalid])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            head = await client.head("/hls/1/video0.jpeg.ts?s=first")
            assert head.status_code == 200 and not head.content
            assert head.headers["content-length"] == "8" and head.headers["content-type"] == "video/mp2t"
            assert calls[0][0] == "HEAD" and probe.closed and not probe.consumed
            ranged = await client.get("/hls/1/video0.jpeg.ts?s=first",
                                      headers={"Range": "bytes=4-7", "If-Range": '"etag"'})
            assert ranged.status_code == 206 and ranged.content == b"data"
            assert ranged.headers["content-range"] == "bytes 4-7/8"
            assert ranged.headers["accept-ranges"] == "bytes" and partial.closed
            assert calls[1][2]["headers"]["Range"] == "bytes=4-7"
            assert calls[1][2]["headers"]["If-Range"] == '"etag"'
            assert calls[1][2]["headers"]["Accept-Encoding"] == "identity"
            response = await client.get("/hls/1/video0.jpeg.ts?s=first", headers={"Range": "bytes=20-"})
            assert response.status_code == 416 and response.headers["content-range"] == "bytes */8"
            assert invalid.closed and len(health) == 2  # 无效 Range 不计为源故障。
    asyncio.run(run())


def test_playlist_cache_rewrites_each_session_and_head_reports_get_length():
    async def run():
        upstream = Response(200, {}, b"#EXTM3U\n#EXTINF:4,\nvideo0.jpeg\n#EXT-X-ENDLIST\n")
        app, calls, _ = relay_app([upstream])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            first = await client.get("/hls/1/1080p/video.m3u8?s=first")
            second = await client.get("/hls/1/1080p/video.m3u8?s=other")
            head = await client.head("/hls/1/1080p/video.m3u8?s=other")
            assert first.status_code == second.status_code == head.status_code == 200
            assert "video0.jpeg.ts?s=first" in first.text and "?s=other" not in first.text
            assert "video0.jpeg.ts?s=other" in second.text and "?s=first" not in second.text
            assert not head.content and head.headers["content-length"] == second.headers["content-length"]
            assert len(calls) == 1 and upstream.closed
    asyncio.run(run())


def test_playlist_uses_gateway_prefix_as_absolute_path():
    async def run():
        upstream = Response(200, {}, b"#EXTM3U\n#EXTINF:4,\nvideo0.jpeg\n#EXT-X-ENDLIST\n")
        app, calls, _ = relay_app([upstream])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            prefixed = await client.get("/hls/1/1080p/video.m3u8?s=first",
                                        headers={"X-Forwarded-Prefix": "/stream/http_resolver/tok/"})
            assert "\n/stream/http_resolver/tok/hls/1/1080p/video0.jpeg.ts?s=first\n" in prefixed.text
            # 格式不对的前缀（跨主机、带 ..）不用，退回相对地址
            for bad in ("//evil.invalid", "http://evil.invalid", "/a/../b"):
                response = await client.get("/hls/1/1080p/video.m3u8?s=first", headers={"X-Forwarded-Prefix": bad})
                assert "\n../1080p/video0.jpeg.ts?s=first\n" in response.text
            assert len(calls) == 1
    asyncio.run(run())


def test_fake_header_segment_keeps_full_fetch_and_does_not_advertise_wrong_length():
    async def run():
        ts = (b"\x47" + b"\0" * 187) * 5
        data = b"\x89PNG\r\n" + ts
        segment = Response(200, {"content-length": str(len(data))}, data)
        probe = Response(200, {"content-length": str(len(data))})
        app, calls, _ = relay_app([segment, probe], fake_header=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/hls/1/video0.jpeg.ts?s=first", headers={"Range": "bytes=10-"})
            assert response.status_code == 200 and response.content == ts
            assert "content-length" not in response.headers and "Range" not in calls[0][2]["headers"]
            head = await client.head("/hls/1/video0.jpeg.ts?s=first")
            assert head.status_code == 200 and not head.content and "content-length" not in head.headers
            assert probe.closed and not probe.consumed
    asyncio.run(run())


def test_playlist_refresh_retries_expired_status_without_caching_error():
    async def run():
        expired = Response(403, {})
        valid = Response(200, {}, b"#EXTM3U\n#EXTINF:4,\nvideo0.jpeg\n#EXT-X-ENDLIST\n")
        app, calls, _ = relay_app([expired, valid])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.get("/hls/1/video.m3u8?s=first")
            assert response.status_code == 200 and "video0.jpeg.ts" in response.text
            assert expired.closed and valid.closed and len(calls) == 2
            assert (await client.get("/hls/1/video.m3u8?s=other")).status_code == 200 and len(calls) == 2
    asyncio.run(run())


def test_managed_stream_sends_head_and_releases_media_slot():
    async def run():
        methods = []

        async def serve(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                methods.append(request.split(b" ", 1)[0])
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        metrics = Metrics()
        session = ManagedSession(Settings(), metrics, "hls", 1)
        try:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/video.ts"
            response = await session.request("HEAD", url, stream=True, timeout=2)
            try:
                assert response.status_code == 200 and response.headers["content-length"] == "8"
                assert b"".join([chunk async for chunk in response.aiter_content()]) == b""
            finally:
                await response.aclose()
            assert methods == [b"HEAD"] and session.active == 0
            assert metrics.gauges["http_active_hls"] == 0
        finally:
            await session.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())
