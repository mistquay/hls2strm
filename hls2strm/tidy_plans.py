"""残留目录检查的持久化清单、按原范围确认和任务结果分页。"""

import asyncio
from collections import Counter
import itertools
import json
import logging
from pathlib import Path
import secrets
import time

from .maintenance_files import entries, validate
from .storage import safe_directory
from .trash import ORPHAN_MIN_AGE, find_orphans, retire

log = logging.getLogger(__name__)


def manifest(manager, job_id, library_id, *, results=False):
    suffix = "results" if results else "plan"
    return manager.e.snapshot_dir.parent / "tidy-plans" / f"{int(job_id)}-{int(library_id)}.{suffix}.jsonl"


def write_plan(path, root, root_id, orphans):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for index, (directory, size) in enumerate(orphans):
            files, folders = entries(directory)
            row = {"index": index, "path": str(directory.relative_to(root)), "bytes": size,
                   "root_identity": root_id, "files": files, "folders": folders}
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    if safe_directory(root) != root_id:
        raise ValueError("检查期间根目录身份改变，请核对挂载")
    temporary.replace(path)


def read_rows(path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def record_result(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
        output.flush()


async def publish(manager, job_id, library_id, info):
    async with manager.db._tx(invalidate=False) as conn:
        job = await manager.db.get_job(job_id)
        state = job["state"]
        state["manifest_version"] = 1
        libraries = state.setdefault("libraries", {})
        libraries[str(library_id)] = info
        for key in ("orphans", "bytes", "moved", "failed", "skipped"):
            state[key] = sum(row.get(key, 0) for row in libraries.values())
        state["partial_failure"] = bool(state["failed"])
        state.setdefault("preview_token", secrets.token_urlsafe(24))
        await conn.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), job_id))


def apply_row(root, root_id, row, days):
    if safe_directory(root) != root_id:
        raise ValueError("清理期间挂载身份改变")
    directory = validate(root, row)
    cutoff = time.time() - ORPHAN_MIN_AGE
    paths = [directory / f["path"] for f in row["files"]] + [directory / rel for rel, _ in row["folders"]]
    if any(p.lstat().st_mtime >= cutoff for p in paths):
        raise ValueError("一天内有改动，可能仍在整理，保留目录")
    return retire(directory, root, days)


async def run(manager, job, task):
    e, lid = manager.e, int(task["target"])
    lib = e.libs.get(lid)
    if not lib or not lib.get("external_dir"):
        if job["params"].get("automatic"):
            return
        raise ValueError("原输出库或外部整理目录已不存在")
    if lib.get("operation_pending"):
        if job["params"].get("automatic"):
            return
        raise ValueError("先完成输出库配置变更")
    root = e.writer.external_root(lib).absolute()
    try:
        root_id = await asyncio.to_thread(safe_directory, root)
    except (OSError, ValueError) as error:
        if not job["params"].get("automatic"):
            raise
        await publish(manager, job["id"], lid, {"name": lib["name"], "root": str(root), "complete": False,
            "orphans": 0, "bytes": 0, "skipped": 1, "error": str(error)})
        log.warning("清理残留「%s」：目录不可达或挂载异常，跳过：%s", lib["name"], error)
        return
    days = e.store.current.trash_days
    source_id = job["params"].get("preview_id", job["id"])
    plan = manifest(manager, source_id, lid)
    if source_id != job["id"]:
        source = await manager.db.get_job(source_id)
        info = source["state"].get("libraries", {}).get(str(lid), {}) if source else {}
        if str(root) != info.get("root") or root_id != info.get("root_identity") or days != info.get("days"):
            raise ValueError("预览后的目录、挂载或回收保留天数已改变，请重新检查")
    else:
        info = job["state"].get("libraries", {}).get(str(lid), {})
        if info and not plan.is_file():
            raise ValueError("原清单已丢失，不能重新扫描扩大清理范围")
        if not info:
            orphans = await asyncio.to_thread(find_orphans, root)
            await asyncio.to_thread(write_plan, plan, root, root_id, orphans)
            info = {"name": lib["name"], "root": str(root), "root_identity": root_id, "days": days,
                    "orphans": len(orphans), "bytes": sum(size for _, size in orphans)}
    if not plan.is_file():
        raise ValueError("原检查清单已不存在，请重新检查")
    if str(root) != info["root"] or root_id != info["root_identity"] or days != info["days"]:
        raise ValueError("原清单的根目录、挂载或回收保留天数已改变")
    info = {**info, "complete": False}
    await publish(manager, job["id"], lid, info)
    if job["params"].get("apply", True):
        result_file = manifest(manager, job["id"], lid, results=True)
        recorded = {r["index"]: r for r in await asyncio.to_thread(lambda: list(read_rows(result_file)))} if result_file.is_file() else {}
        counts = Counter(r["state"] for r in recorded.values())
        iterator = read_rows(plan)
        try:
            from .storage import next_batch
            while batch := await next_batch(iterator):
                for row in batch:
                    await e.checkpoint(job["id"])
                    if row["index"] in recorded:
                        continue
                    result = {"index": row["index"], "state": "skipped", "destination": "", "error": ""}
                    cancelled = False
                    try:
                        async with e.file_changes.exclusive():
                            await e.checkpoint(job["id"])
                            current_lib = e._library(lid)
                            current = e.writer.external_root(current_lib)
                            if current is None or current.absolute() != root or current_lib.get("operation_pending"):
                                raise ValueError("清理期间目录身份或配置改变")
                            worker = asyncio.create_task(asyncio.to_thread(apply_row, root, root_id, row, days))
                            try:
                                target = await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                cancelled = True
                                target = await worker
                        if target is not None:
                            result.update(state="moved", destination=str(target) if days > 0 else "", permanent=days <= 0)
                        else:
                            result["error"] = "目录已不存在或不再是可安全清理的残留"
                    except (OSError, ValueError) as error:
                        result.update(state="failed" if isinstance(error, OSError) and not isinstance(error, FileNotFoundError) else "skipped", error=str(error))
                    await asyncio.to_thread(record_result, result_file, result)
                    counts[result["state"]] += 1
                    if cancelled:
                        info.update({key: counts[key] for key in ("moved", "failed", "skipped")})
                        await publish(manager, job["id"], lid, info)
                        raise asyncio.CancelledError
                info.update({key: counts[key] for key in ("moved", "failed", "skipped")})
                await publish(manager, job["id"], lid, info)
        finally:
            iterator.close()
    info["complete"] = True
    await publish(manager, job["id"], lid, info)
    log.info("残留目录「%s」：检查 %d 个，共 %d 字节，移走 %d，跳过 %d，失败 %d；完整清单见任务 #%d",
             lib["name"], info["orphans"], info["bytes"], info.get("moved", 0), info.get("skipped", 0), info.get("failed", 0), job["id"])


async def approve(manager, preview_id, token):
    db = manager.db
    async with db._tx(invalidate=False) as conn:
        preview = await db.get_job(preview_id)
        if (not preview or preview["kind"] != "tidy" or preview["params"].get("apply", True)
                or preview["status"] != "done" or not token
                or not secrets.compare_digest(token, preview["state"].get("preview_token", ""))):
            raise ValueError("检查预览无效或尚未完成")
        if preview["state"].get("apply_job_id"):
            return preview["state"]["apply_job_id"]
        tasks = await db._all("SELECT target,status FROM tasks WHERE job_id=? ORDER BY id", (preview_id,))
        libraries = preview["state"].get("libraries", {})
        if not tasks or any(t["status"] != "done" or not libraries.get(t["target"], {}).get("complete") for t in tasks):
            raise ValueError("检查有未完成或失败的库，请重新核查")
        if not preview["state"].get("orphans"):
            raise ValueError("没有可清理的残留目录")
        for task in tasks:
            lid = int(task["target"])
            info = libraries[task["target"]]
            lib = manager.e._library(lid)
            root = manager.e.writer.external_root(lib)
            if (lib.get("operation_pending") or root is None or str(root.absolute()) != info["root"]
                    or manager.e.store.current.trash_days != info["days"]
                    or not manifest(manager, preview_id, lid).is_file()):
                raise ValueError("库配置、回收保留天数或原清单改变，请重新检查")
            if await asyncio.to_thread(safe_directory, root.absolute()) != info["root_identity"]:
                raise ValueError("根目录身份已改变，请检查挂载并重新预览")
        stamp = int(time.time())
        params = {"library_id": preview["params"].get("library_id"), "apply": True, "preview_id": preview_id}
        cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES('tidy',?,?,'running',?)",
                                 (f"按检查清理残留 #{preview_id}", json.dumps(params), stamp))
        job_id = cur.lastrowid
        await conn.executemany("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,'tidy',?,20,?)",
                               [(job_id, t["target"], stamp) for t in tasks])
        await conn.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps({**preview["state"], "apply_job_id": job_id}), preview_id))
    manager.e.notify()
    return job_id


def page_rows(manager, job, source, offset, limit):
    libraries = source["state"].get("libraries", {})
    items, total, available = [], 0, bool(libraries)
    for lid, info in sorted(libraries.items()):
        path = manifest(manager, source["id"], lid)
        if not path.is_file():
            available = False
            continue
        count = info["orphans"]
        start, end = max(0, offset - total), min(count, offset + limit - total)
        total += count
        if start >= end:
            continue
        result_file = manifest(manager, job["id"], lid, results=True)
        results = {r["index"]: r for r in read_rows(result_file)} if result_file.is_file() else {}
        with path.open(encoding="utf-8") as plan:
            for line in itertools.islice(plan, start, end):
                row = json.loads(line)
                items.append({"library_id": int(lid), "library_name": info["name"], "path": str(Path(info["root"]) / row["path"]),
                              "bytes": row["bytes"], **results.get(row["index"], {"state": "checked"})})
    return {"items": items, "total": total, "available": available}


async def result(manager, job_id, offset, limit):
    job = await manager.db.get_job(job_id)
    if not job or job["kind"] != "tidy":
        raise ValueError("残留检查任务不存在")
    job["tasks"] = await manager.db.task_counts(job_id)
    source = await manager.db.get_job(job["params"].get("preview_id", job_id))
    if not source:
        raise ValueError("原检查任务已不存在")
    page = await asyncio.to_thread(page_rows, manager, job, source, offset, limit)
    return {**page, "job": job, "preview_id": source["id"], "libraries": source["state"].get("libraries", {}),
            "preview_token": source["state"].get("preview_token"), "apply_job_id": source["state"].get("apply_job_id"),
            "legacy": not source["state"].get("manifest_version")}
