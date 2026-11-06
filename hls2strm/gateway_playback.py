"""网关播放准备：先确认格式，再签发固定会话；失败不返回未选源的地址。"""

import logging
import time
from urllib.parse import urlencode

from fastapi import HTTPException

from .play_history import delivery as history_delivery

log = logging.getLogger(__name__)


async def prepare_playback(request, slug, want, *, mode, relay, ua, origin, fetch_mode,
                           min_remaining, container):
    from .play import NoDirectSource, _resolve_or_http, direct_blocker, playback_source

    ctx = request.app.state.ctx
    settings = ctx.store.current
    ctx.metrics.inc("resolve_requests")
    base = str(request.base_url).rstrip("/") if relay == "gateway" else settings.resolve_proxy_url
    can_relay = bool(base) and mode != "strict_redirect"
    if mode == "proxy" and not can_relay:
        raise HTTPException(409, "播放中转需要公网中转地址，或者网关开启 Relay")
    async def choose(direct_only):
        return await _resolve_or_http(request, slug, want=want, remote=True,
                                      min_remaining=min_remaining, relay=mode == "proxy",
                                      direct_only=direct_only, container=container)

    try:
        resolved = await choose(mode == "redirect" or not can_relay)
    except NoDirectSource:
        if not can_relay:
            raise HTTPException(409, "没有符合当前直连要求的可用源") from None
        resolved = await choose(False)
    except HTTPException as error:
        if mode != "redirect" or not can_relay or error.status_code != 502:
            raise
        resolved = await choose(False)

    actual = resolved.media_container
    source = playback_source(resolved)
    blocked = direct_blocker(settings.proxy_user_agents if resolved.traits.ua_block else [], ua, origin, fetch_mode)
    needs_relay = mode == "proxy" or not resolved.traits.direct or resolved.traits.ip_bound or bool(blocked)
    if needs_relay and not can_relay:
        raise HTTPException(409, "当前源需要中转，请配置公网中转地址或开启网关 Relay")

    # 已经声明格式后，起播恢复只能寻找相同容器，不能再次交付另一种格式。
    resolved.playback_options = {**(resolved.playback_options or {}), "container": actual}
    if needs_relay:
        # 中转固定到已准备的源；可直连的源返回本次确认的 CDN URL，不再次选源。
        session = await ctx.resolver.sessions.persist(ctx.db, resolved)
        query = {"s": session}
        if settings.play_token:
            query["t"] = settings.play_token
        if want is not None:
            query["want"] = str(want)
        location = f"{base}/media/{resolved.source['id']}.{actual}?{urlencode(query)}"
        ttl = min(settings.resolve_relay_ttl or 21600, (resolved.video.get("duration") or 0)+3600)
        history_delivery(request, resolved, mode="proxy", session=session)
        is_relay = True
    else:
        from .play import _pick_variant
        location = await _pick_variant(ctx, resolved, want)
        lifetime = max(settings.play_session_hours*3600, (resolved.video.get("duration") or 0)+3600)
        ttl = max(0, int((resolved.expires or time.time()+lifetime)-time.time()))
        history_delivery(request, resolved, mode="redirect")
        is_relay = False
    log.info("prepare %s：%s %s，容器 %s，%s", slug, resolved.site.label,
             resolved.line["line"] if resolved.line else "", actual, "绑定中转会话" if is_relay else "固定直链")
    return {"protocol_version": 1, "slug": slug, "url": location, "ttl": ttl, "relay": is_relay,
            "container": actual, "media_type": source["media_type"], "source": source,
            "duration": resolved.video.get("duration"), "transcoding": False}
