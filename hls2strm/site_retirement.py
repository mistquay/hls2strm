"""站点清退：彻底不用一个站点。

停用站点和它的订阅，取消排队中的这个站的子任务，再排一个删源任务删掉所有影片在这个站的源：
还有别的能用的源的影片留在库里；没有的下架，文件搬进归档目录（见 engine.remove_sources、archive.py）。
「能用」按 source_policy.eligible 算，别的源所在站点停用、没有启用的线路都不算；只看本地记录，不抓源站、不探测播放。

恢复：启用站点和清退时停用的订阅，按清退任务的子任务（清退了哪些影片）排恢复任务，只恢复这个站的源，
归档的文件搬回原处。
"""

from __future__ import annotations

import json
import time

from .engine import PRIORITY_USER
from .sites import SITES
from .source_policy import eligible

SAMPLE = 50  # 预览列出多少部会下架的
TARGETS = "SELECT DISTINCT video_id FROM sources WHERE site=? AND status!='removed'"


def _site(site: str) -> str:
    if site not in SITES:
        raise ValueError(f"未知站点：{site}")
    return SITES[site].label


async def records(engine) -> list[dict]:
    rows = await engine.db._all(
        "SELECT r.site, r.job_id, r.created_at, j.status, j.name FROM site_retirements r "
        "LEFT JOIN jobs j ON j.id=r.job_id ORDER BY r.job_id DESC")
    return [{**dict(r), "tasks": await engine.db.task_counts(r["job_id"])} for r in rows]


async def _retired(db, site: str) -> None:
    if await db._one("SELECT 1 FROM site_retirements WHERE site=?", (site,)):
        raise ValueError("这个站点已经清退过了，要重新清退先恢复")


async def preview(engine, site: str) -> dict:
    """会怎么处理：多少部留下、多少部下架（按原因分），列出前几部下架的，以及会停掉的订阅、任务。"""
    label, db = _site(site), engine.db
    await _retired(db, site)
    archive_dir = engine.archive_root()
    videos = {r["id"]: dict(r) for r in await db._all(
        f"SELECT id, slug, title FROM videos WHERE id IN ({TARGETS}) ORDER BY id", (site,), bulk=True)}
    sources: dict[int, list[dict]] = {}
    for r in await db._all(f"SELECT * FROM sources WHERE video_id IN ({TARGETS}) AND site!=?", (site, site), bulk=True):
        sources.setdefault(r["video_id"], []).append(dict(r))
    lines: dict[int, list[dict]] = {}
    for r in await db._all(f"""SELECT l.* FROM source_lines l JOIN sources s ON s.id=l.source_id
                               WHERE s.video_id IN ({TARGETS}) AND s.site!=?""", (site, site), bulk=True):
        lines.setdefault(r["source_id"], []).append(dict(r))
    libraries: dict[int, list[str]] = {}
    for r in await db._all(f"""SELECT o.video_id, l.name FROM outputs o JOIN libraries l ON l.id=o.library_id
                               WHERE o.video_id IN ({TARGETS}) ORDER BY l.id""", (site,), bulk=True):
        libraries.setdefault(r["video_id"], []).append(r["name"])
    settings = engine.store.current
    keep, reasons, samples, in_libraries = 0, {}, [], 0
    for vid, v in videos.items():
        others = sources.get(vid, [])
        if any(eligible(s, settings, lines.get(s["id"], [])) for s in others):
            keep += 1
            continue
        if not others:
            reason = "只有这个站的源"
        elif not any(s["status"] == "active" for s in others):
            reason = "别的站的源已下架或删掉"
        else:
            reason = "别的站的源所在站点停用或没有启用的线路"
        reasons[reason] = reasons.get(reason, 0) + 1
        in_libraries += vid in libraries
        if len(samples) < SAMPLE:
            samples.append({**v, "reason": reason, "libraries": libraries.get(vid, [])})
    subs = [s["name"] for s in await db.list_subscriptions() if s["site"] == site and s["enabled"]]
    jobs = [r["name"] for r in await db._all(
        "SELECT DISTINCT j.name FROM jobs j JOIN tasks t ON t.job_id=j.id "
        "WHERE t.site=? AND t.status='pending' AND j.status IN ('running','paused')", (site,))]
    return {"site": site, "label": label, "total": len(videos), "keep": keep, "remove": len(videos) - keep,
            "in_libraries": in_libraries, "reasons": reasons, "samples": samples, "subscriptions": subs,
            "jobs": jobs, "archive_dir": str(archive_dir)}


async def apply(engine, site: str, *, video_ids=None, preview_id=None, parent_job_id=None) -> int:
    """清退，返回删源任务 id。"""
    label, db = _site(site), engine.db
    engine.archive_root()
    parent = await db.get_job(parent_job_id) if parent_job_id else None
    child = parent["state"].get("child_job_id") if parent else None
    if child:
        job = await db.get_job(child)
        if not job or job["params"].get("preview_id")!=preview_id:
            raise ValueError("清退执行记录不可用")
        if job["status"]=="paused":
            await engine.store.update({"sites": {site: {"enabled": False}}})
            await db.update_job(child,status="running")
        for cancelled_job in job["params"].get("cancelled_jobs",[]):
            await engine._maybe_finish_job(cancelled_job)
        await engine._maybe_finish_job(child)
        engine.notify()
        return child
    await _retired(db, site)
    frozen = preview_id is not None and video_ids is None
    ids = [] if frozen else video_ids if video_ids is not None else [r["video_id"] for r in await db._all(TARGETS + " ORDER BY video_id", (site,), bulk=True)]
    total = (await db._one("SELECT COUNT(*) AS n FROM operation_items WHERE preview_id=?",(preview_id,),bulk=True))["n"] if frozen else len(ids)
    stamp = int(time.time())
    # 队列、停用订阅及父任务回执原子保存；删源任务先暂停，设置落盘后才开始。
    async with db._tx() as conn:
        await _retired(db,site)
        subs = [s["id"] for s in await db.list_subscriptions() if s["site"]==site and s["enabled"]]
        jobs = [r["job_id"] for r in await db._all("SELECT DISTINCT job_id FROM tasks WHERE site=? AND status='pending'",(site,))]
        await conn.execute("UPDATE subscriptions SET enabled=0 WHERE site=? AND enabled=1",(site,))
        await conn.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE site=? AND status='pending'",(stamp,site))
        params = {"sites":[site],"subscriptions":subs,"preview_id":preview_id,"cancelled_jobs":jobs}
        cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES('source_remove',?,?,'paused',?)",(f"清退站点 {label}：{total} 部",json.dumps(params),stamp))
        job_id = cur.lastrowid
        if frozen:
            await conn.execute("INSERT INTO tasks(job_id,kind,target,priority,updated_at) SELECT ?,'source_remove',CAST(video_id AS TEXT),?,? FROM operation_items WHERE preview_id=? ORDER BY video_id",(job_id,PRIORITY_USER,stamp,preview_id))
        else:
            await conn.executemany("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,'source_remove',?,?,?)",[(job_id,str(vid),PRIORITY_USER,stamp) for vid in ids])
        await conn.execute("INSERT INTO site_retirements(site,job_id,created_at) VALUES(?,?,?)",(site,job_id,stamp))
        if parent:
            await conn.execute("UPDATE jobs SET state=? WHERE id=?",(json.dumps({**parent["state"],"child_job_id":job_id,"matched":total}),parent_job_id))
    await engine.store.update({"sites": {site: {"enabled": False}}})
    await db.update_job(job_id,status="running")
    for cancelled_job in jobs:
        await engine._maybe_finish_job(cancelled_job)
    await engine._maybe_finish_job(job_id)  # 这个站一部影片都没有时直接结束
    engine.notify()
    return job_id


async def restore(engine, site: str) -> int:
    """恢复清退，返回恢复任务 id。"""
    label, db = _site(site), engine.db
    rec = await db._one("SELECT * FROM site_retirements WHERE site=?", (site,))
    if rec is None:
        raise ValueError("这个站点没有清退记录")
    job = await db.get_job(rec["job_id"])
    if job and job["status"] in ("running", "paused"):
        raise ValueError("清退任务还没结束，先等它跑完或者取消")
    ids = [r["target"] for r in await db._all("SELECT target FROM tasks WHERE job_id=? ORDER BY id", (rec["job_id"],))]
    await engine.store.update({"sites": {site: {"enabled": True}}})
    for sub_id in (job["params"].get("subscriptions", []) if job else []):
        await db.update_subscription(sub_id, enabled=1)
    job_id = await db.create_job("source_restore", f"恢复站点清退 {label}：{len(ids)} 部", {"sites": [site]})
    await db.add_tasks(job_id, "source_restore", ids, PRIORITY_USER)
    await db._write("DELETE FROM site_retirements WHERE site=?", (site,))
    await engine._maybe_finish_job(job_id)
    engine.notify()
    return job_id
