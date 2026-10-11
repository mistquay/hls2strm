import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from hls2strm import http_resources
from hls2strm.config import Settings
from hls2strm.errors import PoolBusy
from hls2strm.http_resources import ManagedSession
from hls2strm.observability import Metrics
from hls2strm.runtime import RequestTelemetry, trace


async def _slow_server():
    """先回响应头和一点内容，之后一直不发完：中转还在传的时候播放器断开。"""
    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10000000\r\n\r\n" + b"x" * 1000)
            await writer.drain()
            await asyncio.sleep(30)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/seg.ts"


def test_cancelled_close_still_returns_curl_handle():
    """收尾被取消（播放器断开）时 curl 句柄也要还回池子；以前漏光后所有中转都卡在取句柄上。"""
    async def run():
        server, url = await _slow_server()
        metrics = Metrics()
        session = ManagedSession(Settings(), metrics, "hls", 1)
        try:
            resp = await session.get(url, stream=True, timeout=5)
            closing = asyncio.create_task(resp.aclose())
            await asyncio.sleep(0)  # 停在等 curl 任务结束的 await 上
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
            assert session.raw.pool.qsize() == 1 and session.active == 0
            assert metrics.gauges["relay_buffer_bytes"] == 0
            again = await asyncio.wait_for(session.get(url, stream=True, timeout=5), 3)
            await again.aclose()
        finally:
            await session.close()
            server.close()
    asyncio.run(run())


def test_exhausted_curl_pool_fails_fast(monkeypatch, caplog):
    monkeypatch.setattr(http_resources, "CURL_WAIT", 0.2)
    caplog.set_level(logging.WARNING, logger="hls2strm")

    async def run():
        metrics = Metrics()
        session = ManagedSession(Settings(), metrics, "hls", 1)
        leaked = await session.raw.pop_curl()  # 模拟漏掉的句柄
        try:
            with pytest.raises(PoolBusy, match="curl 句柄用光了"):
                await session.get("http://127.0.0.1:9/seg.ts", stream=True, timeout=5)
            assert metrics.counters["pool_hls_curl_exhausted"] == 1 and session.active == 0
        finally:
            session.raw.release_curl(leaked)
            await session.close()
    asyncio.run(run())
    assert "curl 句柄用光了" in caplog.text


def _app(timeout: float) -> FastAPI:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(store=SimpleNamespace(current=SimpleNamespace(resolve_timeout=timeout)),
                                    metrics=Metrics())

    @app.get("/hls/{source_id}/{path:path}")
    async def hls(source_id: int, path: str):
        trace(f"源 #{source_id} MissAV abc-001，取 {path}")
        if path.endswith(".ts"):
            trace("请求上游 cdn.example …/1080p/video0.ts")
            await asyncio.sleep(5)
        raise HTTPException(502, "CDN 返回 HTTP 403")

    app.add_middleware(RequestTelemetry)
    return app


def test_playback_5xx_logs_path_and_steps(caplog):
    caplog.set_level(logging.INFO, logger="hls2strm")

    async def run():
        transport = httpx.ASGITransport(app=_app(0.3))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.get("/hls/42/1080p/video.m3u8?s=secret&t=token")
            slow = await client.get("/hls/42/1080p/video0.ts?s=secret")
        return bad, slow

    bad, slow = asyncio.run(run())
    assert bad.status_code == 502 and slow.status_code == 504
    assert "卡在：请求上游 cdn.example" in slow.json()["detail"]
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("/hls/42/1080p/video.m3u8 HTTP 502" in m and "源 #42 MissAV abc-001" in m for m in lines), lines
    assert any("HTTP 504" in m and "请求上游 cdn.example" in m and "总时限" in m for m in lines), lines
    assert not any("secret" in m or "token" in m for m in lines)
