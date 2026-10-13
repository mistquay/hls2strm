"""前缀统计快照、批次计划及幂等抓取结果；不改变影片身份规则。"""

import hashlib
import re
import time
import uuid

from .codes import code_key, edition, is_native, series_prefix
from .operation_plans import selection_query


async def migrate_v25(conn):
    for sql in (
        "CREATE TABLE prefix_scopes(id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, fingerprint TEXT NOT NULL DEFAULT '')",
        """CREATE TABLE prefix_scope_videos(scope_id TEXT NOT NULL, video_id INTEGER NOT NULL,
           prefix TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY(scope_id,video_id))""",
        "CREATE INDEX prefix_scope_series ON prefix_scope_videos(scope_id,prefix)",
        """CREATE TABLE prefix_runs(job_id INTEGER NOT NULL, site TEXT NOT NULL, prefix TEXT NOT NULL,
           source TEXT NOT NULL, block_id TEXT, last_page INTEGER, limited INTEGER NOT NULL DEFAULT 0,
           listing_complete INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY(job_id,site,prefix))""",
        """CREATE TABLE prefix_hits(job_id INTEGER NOT NULL, site TEXT NOT NULL, source_key TEXT NOT NULL,
           prefix TEXT NOT NULL, page INTEGER NOT NULL, video_id INTEGER, new_video INTEGER NOT NULL DEFAULT 0,
           new_source INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL,
           PRIMARY KEY(job_id,site,source_key))""",
        "CREATE INDEX prefix_hits_series ON prefix_hits(job_id,site,prefix)",
    ):
        await conn.execute(sql)


def normalize_prefixes(values):
    out = set()
    for value in values:
        value = value.strip().upper()
        # 手工填写也使用现有数字厂牌别名和 FC2 规范化。
        value = series_prefix(code_key(value + "-123456")) or value
        if not re.fullmatch(r"[A-Z0-9]*[A-Z][A-Z0-9]*", value) or len(value) > 32:
            raise ValueError(f"无效的番号前缀：{value}")
        out.add(value)
    if not out:
        raise ValueError("请至少选择或填写一个前缀")
    return sorted(out)


async def snapshot(db, query, ids=None):
    scope_id = uuid.uuid4().hex
    condition, params = selection_query({"query": query}).where()
    if ids is not None and (not ids or any(type(v) is not int or v < 1 for v in ids)):
        raise ValueError("显式选择必须是非空的影片 ID 列表")
    async with db._tx(invalidate=False) as conn:
        # 未被任何任务引用的统计快照保留一天；抓取任务的快照随记录保留。
        expired = "SELECT id FROM prefix_scopes WHERE created_at<? AND NOT EXISTS(SELECT 1 FROM jobs WHERE json_extract(params,'$.scope_id')=prefix_scopes.id)"
        await conn.execute(f"DELETE FROM prefix_scope_videos WHERE scope_id IN ({expired})", (int(time.time()) - 86400,))
        await conn.execute(f"DELETE FROM prefix_scopes WHERE id IN ({expired})", (int(time.time()) - 86400,))
        await conn.execute("INSERT INTO prefix_scopes(id,created_at) VALUES(?,?)", (scope_id, int(time.time())))
        sql = "INSERT INTO prefix_scope_videos SELECT ?,id,series_prefix(code_key),code FROM videos WHERE "
        if ids is None:
            await conn.execute(sql + condition, (scope_id, *params))
        else:
            ids = sorted(set(ids))
            for offset in range(0, len(ids), 200):
                chunk = ids[offset:offset + 200]
                await conn.execute(sql + f"id IN ({','.join('?' for _ in chunk)})", (scope_id, *chunk))
        fingerprint = hashlib.sha256()
        async with conn.execute("SELECT video_id FROM prefix_scope_videos WHERE scope_id=? ORDER BY video_id", (scope_id,)) as cursor:
            while rows := await cursor.fetchmany(2000):
                for row in rows:
                    fingerprint.update(str(row[0]).encode() + b",")
        await conn.execute("UPDATE prefix_scopes SET fingerprint=? WHERE id=?", (fingerprint.hexdigest(), scope_id))
    return scope_id


async def statistics(db, scope_id, site):
    if not await db._one("SELECT 1 FROM prefix_scopes WHERE id=?", (scope_id,)):
        raise ValueError("统计快照已过期，请重新统计")
    rows = await db._all(
        """SELECT sv.prefix,COUNT(*) AS total,MIN(sv.code) AS sample,
           SUM(EXISTS(SELECT 1 FROM sources s WHERE s.video_id=sv.video_id AND s.site=? AND s.status='active')) AS covered,
           SUM(EXISTS(SELECT 1 FROM sources s WHERE s.video_id=sv.video_id AND s.site=? AND s.status='removed')) AS removed
           FROM prefix_scope_videos sv WHERE sv.scope_id=? GROUP BY sv.prefix ORDER BY total DESC,sv.prefix""",
        (site, site, scope_id), bulk=True)
    unknown = next((r["total"] for r in rows if not r["prefix"]), 0)
    return {"scope_id": scope_id, "site": site, "total": sum(r["total"] for r in rows), "unrecognized": unknown,
            "items": [{**dict(r), "missing": r["total"] - r["covered"]} for r in rows if r["prefix"]]}


async def matching_video(db, site, item, scope_id=None):
    # 已关联的站内页面优先，避免同番号旧重复记录导致挂错影片。
    source = await db.find_source(site, item.key)
    if source:
        video = await db.get_video_by_id(source["video_id"])
        if scope_id and not await db._one("SELECT 1 FROM prefix_scope_videos WHERE scope_id=? AND video_id=?", (scope_id, source["video_id"])):
            return None
        return video
    key = code_key(item.code)
    condition = "v.code_key=?" if is_native(key) else "v.code_key=? AND v.uncensored=?"
    args = [key] if is_native(key) else [key, int(edition(item.code, item.uncensored))]
    if scope_id:
        condition += " AND EXISTS(SELECT 1 FROM prefix_scope_videos sv WHERE sv.scope_id=? AND sv.video_id=v.id)"
        args.append(scope_id)
    row = await db._one(f"SELECT v.id FROM videos v WHERE {condition} ORDER BY v.status='active' DESC,v.id LIMIT 1", args)
    return await db.get_video_by_id(row["id"]) if row else None


async def record_skip(db, job_id, site, prefix, page, item, outcome):
    await db._write("INSERT OR IGNORE INTO prefix_hits(job_id,site,source_key,prefix,page,outcome) VALUES(?,?,?,?,?,?)",
                    (job_id, site, item.key, prefix, page, outcome), invalidate=False)


async def progress(db, job_id):
    runs = await db._all("SELECT * FROM prefix_runs WHERE job_id=? ORDER BY site,prefix", (job_id,), bulk=True)
    counts = await db._all(
        """SELECT site,prefix,COUNT(DISTINCT video_id) AS seen,
           COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
           SUM(new_source) AS sources_added,SUM(outcome='mismatch') AS mismatch,
           SUM(outcome='outside') AS outside,SUM(outcome='removed') AS removed,
           SUM(outcome='excluded') AS excluded
           FROM prefix_hits WHERE job_id=? GROUP BY site,prefix""", (job_id,), bulk=True)
    by_run = {(r["site"], r["prefix"]): dict(r) for r in counts}
    tasks = await db._all(
        """SELECT site,substr(target,instr(target,'/')+1,instr(substr(target,instr(target,'/')+1),'/')-1) AS prefix,
           SUM(kind='prefix_list' AND status='done') AS pages_done,
           SUM(status='failed' OR status='gone') AS failed,
           SUM(status='pending') AS pending,SUM(status='running') AS running,SUM(status='cancelled') AS cancelled_tasks
           FROM tasks WHERE job_id=? AND kind IN ('prefix_list','prefix_detail') GROUP BY site,prefix""", (job_id,), bulk=True)
    by_task = {(r["site"], r["prefix"]): dict(r) for r in tasks}
    items = []
    for row in runs:
        item = {**dict(row), **by_run.get((row["site"], row["prefix"]), {}), **by_task.get((row["site"], row["prefix"]), {})}
        item["scan_status"] = ("cancelled" if item["cancelled"] or item.get("cancelled_tasks") else
                               "failed" if item.get("failed") else
                               "limited" if item["listing_complete"] and item["limited"] else
                               "complete" if item["listing_complete"] else "scanning")
        items.append(item)
    summary = await db._one(
        """SELECT COUNT(DISTINCT video_id) AS seen,COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
           COALESCE(SUM(new_source),0) AS sources_added,
           COALESCE(SUM(outcome!='accepted'),0) AS skipped FROM prefix_hits WHERE job_id=?""", (job_id,), bulk=True)
    return {"items": items, "summary": dict(summary)}
