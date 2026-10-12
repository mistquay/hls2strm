"""播放交付记录：每个会话一条，不记录分片、探测或敏感播放地址。"""

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict

from starlette.requests import Request

from .runtime import request_id

log = logging.getLogger(__name__)


async def migrate_v20(conn):
    await conn.execute("""CREATE TABLE play_history (
        id INTEGER PRIMARY KEY, session_key TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        slug TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
        video_id INTEGER, source_id INTEGER, site TEXT NOT NULL DEFAULT '',
        source_key TEXT NOT NULL DEFAULT '', line TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '', entry TEXT NOT NULL, state TEXT NOT NULL,
        http_status INTEGER NOT NULL, client TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '')""")
    await conn.execute("CREATE INDEX play_history_recent ON play_history(created_at DESC,id DESC)")
    await conn.execute("CREATE INDEX play_history_mode ON play_history(mode,created_at DESC,id DESC)")
    await conn.execute("CREATE INDEX play_history_site ON play_history(site,created_at DESC,id DESC)")


def attempt(request, slug):
    linked = request.query_params.get("s") if request.url.path.startswith("/media/") else request.query_params.get("ph")
    linked = linked if linked and re.fullmatch(r"[a-f0-9]{24,48}",linked) else ""
    entry = "gateway" if request.url.path.startswith("/api/resolve/") else "session" if request.url.path.startswith("/media/") else "play"
    request.scope["play_history"] = {"session_key":linked or secrets.token_hex(24),"slug":slug,"entry":entry,"linked":bool(linked)}


def delivery(request, resolved=None, *, mode="proxy", session=None):
    info = request.scope.get("play_history")
    if info is None:
        attempt(request,resolved.video["slug"] if resolved else "")
        info = request.scope["play_history"]
    info["mode"] = mode
    if session and not info["linked"]:
        info["session_key"] = session
    if resolved:
        info.update(slug=resolved.video["slug"],video_id=resolved.video["id"],code=resolved.video.get("code") or "",
                    title=resolved.video.get("title") or "",source_id=resolved.source["id"],site=resolved.site.name,
                    source_key=resolved.source["key"],line=resolved.line["line"] if resolved.line else "",host=resolved.host)
    return info["session_key"]


class PlaybackHistory:
    def __init__(self, db, store):
        self.db,self.store = db,store
        self.queue = asyncio.Queue(maxsize=store.current.play_history_pending)
        self.seen = OrderedDict()
        self.worker = None
        self.dropped = 0

    def start(self):
        self.worker = asyncio.create_task(self._run(),name="play-history")

    def submit(self, record):
        if not self.store.current.play_history_enabled:
            return
        key = record["session_key"]
        signature = (record.get("source_id"),record.get("line"),record["mode"],record["state"],record["http_status"] if record["state"]=="failed" else 0)
        if self.seen.get(key)==signature:
            self.seen.move_to_end(key)
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped==1 or self.dropped%100==0:
                log.warning("播放记录队列已满，累计未写入 %d 条；播放交付继续",self.dropped)
            return
        self.seen[key] = signature
        while len(self.seen)>self.store.current.play_history_pending:
            self.seen.popitem(last=False)

    async def _run(self):
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                await self.write(item)
            except Exception:
                self.seen.pop(item["session_key"],None)
                log.exception("播放记录写入失败，播放交付不受影响")
            finally:
                self.queue.task_done()

    async def write(self, record):
        record = {k:v for k,v in record.items() if k!="linked"}
        columns = tuple(record)
        updates = [f"{key}=excluded.{key}" for key in columns if key not in ("session_key","created_at","entry","client","user_agent")]
        # 同一会话收到 Range 或重新拉清单，更新实际交付；保留首次入口和客户端。
        async with self.db._tx(invalidate=False) as conn:
            await conn.execute(f"INSERT INTO play_history({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(session_key) DO UPDATE SET {','.join(updates)} WHERE play_history.slug=excluded.slug",tuple(record.values()))

    async def stop(self):
        if self.worker:
            await self.queue.put(None)
            await self.worker
            self.worker = None

    async def cleanup(self):
        settings = self.store.current
        removed = 0
        async with self.db._tx(invalidate=False) as conn:
            if settings.play_history_days:
                cur = await conn.execute("DELETE FROM play_history WHERE created_at<?",(int(time.time())-settings.play_history_days*86400,))
                removed += cur.rowcount
            if settings.play_history_max_entries:
                cur = await conn.execute("DELETE FROM play_history WHERE id IN (SELECT id FROM play_history ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?)",(settings.play_history_max_entries,))
                removed += cur.rowcount
        return {"removed": removed}

    async def page(self, *, mode="",site="",state="",q="",since=None,until=None,limit=50,offset=0):
        where,params = ["1=1"],[]
        for key,value in (("mode",mode),("site",site),("state",state)):
            if value:
                where.append(f"{key}=?")
                params.append(value)
        if q.strip():
            where.append("(slug LIKE ? OR code LIKE ? OR title LIKE ?)")
            params.extend([f"%{q.strip()}%"]*3)
        for clause,value in (("created_at>=?",since),("created_at<?",until)):
            if value is not None:
                where.append(clause)
                params.append(value)
        condition = " AND ".join(where)
        counts = await self.db._one(f"SELECT COUNT(*) AS total,COALESCE(SUM(mode='redirect'),0) AS redirect,COALESCE(SUM(mode='proxy'),0) AS proxy,COALESCE(SUM(state IN ('failed','interrupted')),0) AS failed FROM play_history WHERE {condition}",params,bulk=True)
        columns = "id,created_at,updated_at,slug,code,title,video_id,source_id,site,source_key,line,host,mode,entry,state,http_status,client,user_agent,request_id"
        rows = await self.db._all(f"SELECT {columns} FROM play_history WHERE {condition} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",(*params,limit,offset),bulk=True)
        return {"items":[dict(row) for row in rows],"total":counts["total"],"counts":dict(counts)}


class PlaybackHistoryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"]!="http" or scope.get("method")!="GET" or not scope["path"].startswith(("/play/","/media/","/api/resolve/")):
            return await self.app(scope,receive,send)
        request = Request(scope)
        ctx = getattr(scope["app"].state,"ctx",None)
        if not ctx or not ctx.store.current.play_history_enabled or request.query_params.get("prepare") in ("1","true","True"):
            return await self.app(scope,receive,send)
        status,record = 500,None

        async def observed(message):
            nonlocal status,record
            if message["type"]=="http.response.start":
                status = message["status"]
                info = scope.get("play_history")
                if info and not scope.get("skip_play_history") and status not in (401,403,422):
                    stamp = int(time.time())
                    record = {**info,"mode":info.get("mode",""),"created_at":stamp,"updated_at":stamp,
                              "state":"failed" if status>=400 else "issued" if info["entry"]=="gateway" else "delivered",
                              "http_status":status,"client":request.client.host if request.client else "",
                              "user_agent":(request.query_params.get("ua") if info["entry"]=="gateway" else request.headers.get("user-agent","")) or "",
                              "request_id":request_id.get()}
                    record["user_agent"] = record["user_agent"][:512]
                    ctx.play_history.submit(record)
            await send(message)
        try:
            await self.app(scope,receive,observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            if record and status<400:
                ctx.play_history.submit({**record,"state":"interrupted","updated_at":int(time.time())})
            raise
