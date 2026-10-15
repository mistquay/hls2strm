"""任务列表、目标检索、阶段和实际执行结果；旧数组接口仍可使用。"""

import json
import time

from .sites import SITES
from .task_controls import CANCELLABLE_KINDS


async def remaining_tasks(db, jobs, *, cached=False):
    """一页任务共用一次汇总查询，只读取未结束子任务。"""
    ids = [j["id"] for j in jobs if j["status"] in ("running", "paused")]
    if not ids:
        return {}
    if cached:
        return await db.job_cache.get(("remaining", tuple(ids)), lambda: remaining_tasks(db, jobs), 5)
    rows = await db._all(
        f"""SELECT job_id,kind,site,SUM(status='pending') AS pending,SUM(status='running') AS running,
                   MIN(CASE WHEN status='pending' THEN next_run_at END) AS next_run_at
            FROM tasks WHERE job_id IN ({','.join('?' * len(ids))}) AND status IN ('pending','running')
            GROUP BY job_id,kind,site ORDER BY job_id,kind,site""", ids, bulk=True)
    grouped = {}
    for row in rows:
        grouped.setdefault(row["job_id"], []).append(dict(row))
    return grouped


def remaining_text(engine, groups):
    names = {"list": "列表抓取", "detail": "详情", "probe": "补源", "cover": "封面", "quality": "画质探测", "prefix_list": "前缀列表", "prefix_detail": "前缀详情"}
    parts = []
    now = time.time()
    for group in groups:
        site = group["site"]
        label = SITES[site].label if site in SITES else site
        count = group["pending"] + group["running"]
        part = f"{label + ' ' if label else ''}{names.get(group['kind'], group['kind'])} {count} 项"
        if group["running"]:
            part += f"，{group['running']} 项执行中"
        elif site and not engine.store.current.site(site).enabled:
            part += "，站点已停用"
        elif engine.blocked.get(site, 0) > now:
            part += f"，站点冷却，约 {max(1, int(engine.blocked[site] - now))} 秒后重试"
        elif (group["next_run_at"] or 0) > now:
            part += f"，约 {max(1, int(group['next_run_at'] - now))} 秒后重试"
        else:
            part += "，等待调度"
        parts.append(part)
    return "；".join(parts)


def job_condition(db, status="", kind="", q=""):
    where, params = ["1=1"], []
    if status in db.JOB_FILTERS:
        where.append(db.JOB_FILTERS[status])
    if kind:
        where.append("kind=?")
        params.append(kind)
    if q.strip():
        where.append("(name LIKE ? OR id=?)")
        params.extend((f"%{q.strip()}%", q.strip()))
    return " AND ".join(where), params


async def describe(engine, job, remaining=None):
    tasks = job["tasks"]
    if job["status"] == "done":
        job["phase"] = ("partial_cancelled" if tasks.get("cancelled") and not tasks.get("failed")
                        else "partial_failure" if job["state"].get("partial_failure") else "done")
    elif job["status"] != "running":
        job["phase"] = job["status"]
    elif engine.paused:
        job["phase"] = "pausing" if tasks.get("running") else "scheduler_paused"
    elif tasks.get("running"):
        job["phase"] = "executing"
    else:
        job["phase"] = "queued"
    job["phase_detail"] = ""
    job["pending_groups"] = []
    if job["status"] in ("running", "paused"):
        if remaining is None:
            remaining = (await remaining_tasks(engine.db, [job])).get(job["id"], [])
        job["pending_groups"] = [{"kind": g["kind"], "site": g["site"], "count": g["pending"]}
                                 for g in remaining if g["pending"] and g["kind"] in CANCELLABLE_KINDS]
        list_complete = (job["kind"] in ("crawl", "incremental") and job["state"].get("list_complete")
                         and not any(g["kind"] == "list" for g in remaining))
        if list_complete:
            site = job["params"].get("site", "jable")
            label = SITES[site].label if site in SITES else site
            job["phase_detail"] = f"{label} 列表抓取已完成。"
            if remaining and job["status"] == "running" and not engine.paused:
                job["phase"] = "background_running" if tasks.get("running") else "background_waiting"
        if remaining:
            if job["status"] == "paused" or engine.paused:
                job["phase_detail"] += "任务已暂停；剩余："
            elif list_complete:
                job["phase_detail"] += "后续处理："
            job["phase_detail"] += remaining_text(engine, remaining)
    if job["kind"] == "maintenance_scan":
        state = job["state"]
        phases = {"queued": "等待调度", "waiting_lock": "等待前一个扫描结束", "preparing": "准备影片索引",
                  "walking": "扫描文件与 NFO", "trash": "统计回收区", "reconciling": "核对磁盘与数据库记录",
                  "publishing": "发布扫描结果", "archive": "统计归档空间", "pruning": "清理旧索引", "done": "扫描结束"}
        phase = state.get("scan_phase")
        if phase in phases:
            if job["status"] == "running" and not engine.paused and phase in ("queued", "waiting_lock"):
                job["phase"] = "queued"
            prefix = "任务已暂停；" if job["status"] == "paused" or engine.paused else ""
            job["phase_detail"] = (f"{prefix}{phases[phase]}；已扫描 {state.get('assets', 0)} 个目录、"
                                   f"{state.get('files', 0)} 个文件；已核对 {state.get('outputs_checked', 0)} 条记录")
            if state.get("root"):
                job["phase_detail"] += f"；目录：{state['root']}"
    if job["state"].get("child_job_id"):
        child = await engine.db.get_job(job["state"]["child_job_id"])
        if child:
            if child["status"] in ("running","paused") and not job["state"].get("partial_failure") and job["status"]!="cancelled":
                job["phase"] = "following_up"
            elif child["state"].get("partial_failure") or child["status"]=="cancelled":
                job["phase"] = "partial_failure"
    job["state"] = {k:v for k,v in job["state"].items() if not k.startswith("_")}
    return job


async def _summary(db, job_id):
    tasks = await db.task_counts(job_id)
    crawl = (await db.history.counts([job_id])).get(job_id)
    row = await db._one("SELECT MIN(next_run_at) AS next_at FROM tasks WHERE job_id=? AND status='pending'", (job_id,), bulk=True)
    results = await db._all("SELECT result,COUNT(*) AS n FROM tasks WHERE job_id=? AND status='done' AND result!='{}' GROUP BY result", (job_id,), bulk=True)
    summary = {}
    for result in results:
        for key, value in json.loads(result["result"]).items():
            if isinstance(value, int):
                summary[key] = summary.get(key, 0) + value * result["n"]
    remaining = (await remaining_tasks(db, [{"id": job_id, "status": "running"}])).get(job_id, [])
    return {"tasks": tasks, "crawl": crawl, "next_retry_at": row["next_at"] if row else None, "results": summary, "_remaining": remaining}


async def details(engine, job, *, summary_only=False):
    db = engine.db
    if summary_only:
        job.update(await db.job_cache.get(("summary", job["id"], job["status"]), lambda: _summary(db, job["id"]), 5))
    else:
        job.update(await _summary(db, job["id"]))
    await describe(engine, job, remaining=job.pop("_remaining"))
    if job["phase"] == "queued" and job["next_retry_at"] and job["next_retry_at"] > time.time():
        job["phase"] = "waiting_retry"
    if job["kind"] == "prefix_crawl" and not summary_only:
        from .prefix_store import progress
        job["prefix_progress"] = await progress(db, job["id"])
    return job


async def task_page(db, job_id, status="", q="", limit=100, offset=0):
    where, params = ["job_id=?"], [job_id]
    if status:
        where.append("status=?")
        params.append(status)
    if q.strip():
        where.append("(target LIKE ? OR id=?)")
        params.extend((f"%{q.strip()}%", q.strip()))
    condition = " AND ".join(where)
    total = (await db._one(f"SELECT COUNT(*) AS n FROM tasks WHERE {condition}", params, browse=True))["n"]
    rows = await db._all(f"SELECT * FROM tasks WHERE {condition} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", (*params,limit,offset), browse=True)
    items = [{**dict(row), "result": json.loads(row["result"])} for row in rows]
    return {"items": items, "total": total}
