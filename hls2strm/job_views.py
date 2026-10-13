"""任务列表、目标检索、阶段和实际执行结果；旧数组接口仍可使用。"""

import json
import time

from .sites import SITES
from .task_controls import CANCELLABLE_KINDS


async def remaining_tasks(db, jobs):
    """一页任务共用一次汇总查询，只读取未结束子任务。"""
    ids = [j["id"] for j in jobs if j["status"] in ("running", "paused")]
    if not ids:
        return {}
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
    if job["state"].get("child_job_id"):
        child = await engine.db.get_job(job["state"]["child_job_id"])
        if child:
            if child["status"] in ("running","paused") and not job["state"].get("partial_failure") and job["status"]!="cancelled":
                job["phase"] = "following_up"
            elif child["state"].get("partial_failure") or child["status"]=="cancelled":
                job["phase"] = "partial_failure"
    job["state"] = {k:v for k,v in job["state"].items() if not k.startswith("_")}
    return job


async def details(engine, job):
    db = engine.db
    job["tasks"] = await db.task_counts(job["id"])
    job["crawl"] = (await db.history.counts([job["id"]])).get(job["id"])
    row = await db._one("SELECT MIN(next_run_at) AS next_at FROM tasks WHERE job_id=? AND status='pending'", (job["id"],), bulk=True)
    job["next_retry_at"] = row["next_at"] if row else None
    await describe(engine, job)
    if job["phase"] == "queued" and job["next_retry_at"] and job["next_retry_at"] > time.time():
        job["phase"] = "waiting_retry"
    results = await db._all("SELECT result,COUNT(*) AS n FROM tasks WHERE job_id=? AND status='done' AND result!='{}' GROUP BY result", (job["id"],), bulk=True)
    summary = {}
    for row in results:
        for key, value in json.loads(row["result"]).items():
            if isinstance(value, int):
                summary[key] = summary.get(key, 0) + value * row["n"]
    job["results"] = summary
    if job["kind"] == "prefix_crawl":
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
    total = (await db._one(f"SELECT COUNT(*) AS n FROM tasks WHERE {condition}", params, bulk=True))["n"]
    rows = await db._all(f"SELECT * FROM tasks WHERE {condition} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", (*params,limit,offset), bulk=True)
    items = [{**dict(row), "result": json.loads(row["result"])} for row in rows]
    return {"items": items, "total": total}
