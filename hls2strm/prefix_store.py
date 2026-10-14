"""前缀统计快照、批次计划及幂等抓取结果；不改变影片身份规则。"""

import re

from .codes import code_key, edition, is_native, series_prefix

TASK_PREFIX = "substr(target,instr(target,'/')+1,instr(substr(target,instr(target,'/')+1),'/')-1)"

# 每个批次只领取第一个仍有列表/详情工作的站点前缀，包括等待重试的任务。
# +r.prefix 去掉列的 TEXT affinity，让 SQLite 使用完整的表达式索引。
PREFIX_HEADS_SQL = f"""WITH prefix_heads AS MATERIALIZED (
    SELECT j.id AS job_id, (
        SELECT r.site || '/' || r.prefix FROM prefix_runs r
        WHERE r.job_id=j.id AND EXISTS (
            SELECT 1 FROM tasks p INDEXED BY tasks_prefix_progress
            WHERE p.job_id=r.job_id AND p.site=r.site
              AND {TASK_PREFIX}=+r.prefix
              AND p.kind IN ('prefix_list','prefix_detail') AND p.status IN ('pending','running'))
        ORDER BY r.site,r.prefix LIMIT 1
    ) AS head FROM jobs j WHERE j.kind='prefix_crawl' AND j.status='running'
)"""
PREFIX_CLAIM_SQL = f"""AND (t.kind NOT IN ('prefix_list','prefix_detail') OR (
    t.site || '/' || {TASK_PREFIX.replace('target', 't.target')} =
        (SELECT head FROM prefix_heads WHERE job_id=t.job_id)
    AND NOT EXISTS (
        SELECT 1 FROM tasks busy WHERE busy.job_id=t.job_id AND busy.status='running'
          AND busy.kind IN ('prefix_list','prefix_detail')
          AND busy.site || '/' || {TASK_PREFIX.replace('target', 'busy.target')} !=
              t.site || '/' || {TASK_PREFIX.replace('target', 't.target')}
    )
))"""


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


async def snapshot(db, query, ids=None, **options):
    from .prefix_snapshot import build
    return await build(db, query, ids, **options)


async def statistics(db, scope_id, site):
    if not await db._one("SELECT 1 FROM prefix_scopes WHERE id=? AND ready=1", (scope_id,)):
        raise ValueError("统计快照尚未完成或已过期，请重新统计")
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


def _progress_items(runs, counts, tasks):
    by_run = {(r["site"], r["prefix"]): dict(r) for r in counts}
    by_task = {(r["site"], r["prefix"]): dict(r) for r in tasks}
    items = []
    for row in runs:
        key = row["site"], row["prefix"]
        item = {**dict(row), **by_run.get(key, {}), **by_task.get(key, {})}
        item["scan_status"] = ("cancelled" if item["cancelled"] or item.get("cancelled_tasks") else
                               "failed" if item.get("failed") else
                               "limited" if item["listing_complete"] and item["limited"] else
                               "complete" if item["listing_complete"] else
                               "scanning" if item.get("running") else
                               "waiting" if item.get("started") or item["last_page"] else "queued")
        items.append(item)
    return items


async def progress(db, job_id):
    runs = await db._all("SELECT * FROM prefix_runs WHERE job_id=? ORDER BY site,prefix", (job_id,), bulk=True)
    counts = await db._all(
        """SELECT site,prefix,COUNT(DISTINCT video_id) AS seen,
           COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
           SUM(new_source) AS sources_added,SUM(outcome='mismatch') AS mismatch,
           SUM(outcome='outside') AS outside,SUM(outcome='removed') AS removed,
           SUM(outcome='excluded') AS excluded
           FROM prefix_hits WHERE job_id=? GROUP BY site,prefix""", (job_id,), bulk=True)
    tasks = await db._all(
        """SELECT site,substr(target,instr(target,'/')+1,instr(substr(target,instr(target,'/')+1),'/')-1) AS prefix,
           SUM(kind='prefix_list' AND status='done') AS pages_done,
           SUM(status='failed' OR status='gone') AS failed,
           SUM(status='pending') AS pending,SUM(status='running') AS running,SUM(status='cancelled') AS cancelled_tasks,
           SUM(attempts>0 OR status='done' OR last_error!='') AS started
           FROM tasks WHERE job_id=? AND kind IN ('prefix_list','prefix_detail') GROUP BY site,prefix""", (job_id,), bulk=True)
    summary = await db._one(
        """SELECT COUNT(DISTINCT video_id) AS seen,COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
           COALESCE(SUM(new_source),0) AS sources_added,
           COALESCE(SUM(outcome!='accepted'),0) AS skipped FROM prefix_hits WHERE job_id=?""", (job_id,), bulk=True)
    return {"items": _progress_items(runs, counts, tasks), "summary": dict(summary)}


async def progress_summary(db, job_id):
    async def load():
        row = await db._one(
            """SELECT COUNT(DISTINCT video_id) AS seen,
               COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
               COALESCE(SUM(new_source),0) AS sources_added,
               COALESCE(SUM(outcome!='accepted'),0) AS skipped FROM prefix_hits WHERE job_id=?""",
            (job_id,), bulk=True)
        return dict(row)
    return await db.job_cache.get(("prefix_summary", job_id), load, 5)


async def sequence_progress(db, job_id):
    async def load():
        rows = await db._all(f"""
            WITH work AS (
                SELECT site,{TASK_PREFIX} AS prefix,
                    SUM(status IN ('pending','running')) AS open,
                    SUM(status IN ('failed','gone')) AS failed,
                    SUM(status='cancelled') AS cancelled
                FROM tasks WHERE job_id=? AND kind IN ('prefix_list','prefix_detail') GROUP BY site,prefix
            )
            SELECT r.site,r.prefix,r.cancelled,COALESCE(w.open,0) AS open,
                COALESCE(w.failed,0) AS failed,COALESCE(w.cancelled,0) AS cancelled_tasks
            FROM prefix_runs r LEFT JOIN work w ON w.site=r.site AND w.prefix=r.prefix
            WHERE r.job_id=? ORDER BY r.site,r.prefix
        """, (job_id, job_id), bulk=True)
        current = next(({"site": r["site"], "prefix": r["prefix"], "position": i}
                        for i, r in enumerate(rows, 1) if r["open"]), None)
        finished = sum(not r["open"] for r in rows)
        return {"total": len(rows), "finished": finished,
                "waiting": len(rows) - finished - int(current is not None),
                "failed": sum(bool(r["failed"]) for r in rows),
                "cancelled": sum(bool(r["cancelled"] or r["cancelled_tasks"]) for r in rows),
                "current": current}
    return await db.job_cache.get(("prefix_sequence", job_id), load, 3)


async def progress_page(db, job_id, q="", limit=50, offset=0, state=""):
    async def load():
        search = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args = (job_id, f"%{search}%", limit, offset)
        task_match = f"""SELECT 1 FROM tasks t INDEXED BY tasks_prefix_progress
            WHERE t.job_id=r.job_id AND t.site=r.site AND {TASK_PREFIX.replace('target', 't.target')}=+r.prefix
              AND t.kind IN ('prefix_list','prefix_detail')"""
        state_sql = {
            "": "",
            "unfinished": f" AND EXISTS ({task_match} AND t.status IN ('pending','running'))",
            "queued": f" AND r.cancelled=0 AND r.last_page IS NULL AND EXISTS ({task_match} AND t.status='pending') AND NOT EXISTS ({task_match} AND (t.attempts>0 OR t.status!='pending' OR t.last_error!=''))",
            "failed": f" AND EXISTS ({task_match} AND t.status IN ('failed','gone'))",
        }[state]
        where = "r.job_id=? AND r.prefix LIKE ? ESCAPE '\\'" + state_sql
        page_sql = f"SELECT r.* FROM prefix_runs r WHERE {where} ORDER BY r.site,r.prefix LIMIT ? OFFSET ?"
        runs = await db._all(page_sql, args, browse=True)
        total = (await db._one(f"SELECT COUNT(*) AS n FROM prefix_runs r WHERE {where}", args[:2], browse=True))["n"]
        if not runs:
            return {"items": [], "total": total}
        # 每个前缀使用参数精确查找。表达式索引与 TEXT 列关联时可能只用到 job/site，
        # 参数匹配则能使用完整的 job/site/prefix 索引，工作量只随本页增长。
        keys = [(row["site"], row["prefix"], job_id, row["site"], row["prefix"]) for row in runs]
        counts = await db._all(
            " UNION ALL ".join("""SELECT ? AS site,? AS prefix,COUNT(DISTINCT video_id) AS seen,
                       COUNT(DISTINCT CASE WHEN new_video=1 THEN video_id END) AS new,
                       SUM(new_source) AS sources_added,SUM(outcome='mismatch') AS mismatch,
                       SUM(outcome='outside') AS outside,SUM(outcome='removed') AS removed,
                       SUM(outcome='excluded') AS excluded
                FROM prefix_hits WHERE job_id=? AND site=? AND prefix=?""" for _ in runs),
            [value for key in keys for value in key], browse=True)
        tasks = await db._all(
            " UNION ALL ".join(f"""SELECT ? AS site,? AS prefix,
                       SUM(kind='prefix_list' AND status='done') AS pages_done,
                       SUM(status='failed' OR status='gone') AS failed,
                       SUM(status='pending') AS pending,SUM(status='running') AS running,
                       SUM(status='cancelled') AS cancelled_tasks,SUM(attempts>0 OR status='done' OR last_error!='') AS started
                FROM tasks INDEXED BY tasks_prefix_progress
                WHERE job_id=? AND site=? AND {TASK_PREFIX}=?
                  AND kind IN ('prefix_list','prefix_detail')""" for _ in runs),
            [value for key in keys for value in key], browse=True)
        return {"items": _progress_items(runs, counts, tasks), "total": total}
    return await db.job_cache.get(("prefix_page", job_id, q.strip(), limit, offset, state), load, 3)
