"""任务列表、目标检索、阶段和实际执行结果；旧数组接口仍可使用。"""

import json
import time


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


async def describe(engine, job):
    tasks = job["tasks"]
    if job["status"] == "done":
        job["phase"] = "partial_failure" if job["state"].get("partial_failure") else "done"
    elif job["status"] != "running":
        job["phase"] = job["status"]
    elif engine.paused:
        job["phase"] = "pausing" if tasks.get("running") else "scheduler_paused"
    elif tasks.get("running"):
        job["phase"] = "executing"
    else:
        job["phase"] = "queued"
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
