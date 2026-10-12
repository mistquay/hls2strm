"""HLS 原始子清单缓存；改写后的会话地址不进入共享缓存。"""

from .fetcher import close_stream


class HLSUpstreamStatus(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"CDN 返回 HTTP {status}")


async def playlist_bytes(ctx, url: str, headers: dict):
    settings = ctx.store.current

    async def load():
        ctx.metrics.inc("playlist_fetch")
        response = await getattr(ctx.fetcher, "hls_session", ctx.fetcher.session).get(
            url, stream=True, timeout=settings.request_timeout, headers=headers)
        try:
            if response.status_code != 200:
                raise HLSUpstreamStatus(response.status_code)
            return b"".join([chunk async for chunk in response.aiter_content()])
        finally:
            await close_stream(response)

    cache = getattr(ctx.fetcher, "playlists", None)
    if cache is None:
        return await load()
    cache.capacity = settings.playlist_cache_entries
    key = (url, tuple(sorted(headers.items())), settings.proxy, settings.impersonate)
    return await cache.get(key, load, settings.playlist_cache_seconds)
