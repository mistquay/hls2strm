"""已认证的维护 API；只接受配置库、资产 ID 和固定预览令牌。"""

import json
import asyncio
import shutil
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .api import _ctx
from .maintenance_plan import CleanupRequest
from .maintenance_query import AssetFilter, list_assets
from .request_jobs import execute

router = APIRouter()


class ScanRequest(BaseModel):
    library_ids: list[int] = Field(default_factory=list)
    force: bool = False


class Confirm(BaseModel):
    preview_token: str = Field(min_length=1)


class PolicyRequest(BaseModel):
    missing_policy: Literal["restore", "review"]


class BindingRequest(BaseModel):
    video_id: int = Field(gt=0)


@router.post("/maintenance/assets/{asset_id}/bind")
async def bind_asset(asset_id: int, body: BindingRequest, request: Request):
    try:
        await _ctx(request).engine.maintenance.bind_asset(asset_id, body.video_id)
        return {"ok": True}
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None


@router.get("/maintenance/overview")
async def overview(request: Request):
    c = _ctx(request)
    roots = []
    configured = {(l["id"], str(r)) for l in c.engine.libs.values() for r in c.engine.maintenance.roots(l)}
    published = await c.db._all("SELECT * FROM maintenance_roots ORDER BY library_id,root", browse=True)
    for row in published:
        if (row["library_id"], row["root"]) not in configured:
            continue
        item = dict(row)
        item["stats"] = json.loads(item["stats"])
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, item["root"])
            item.update(disk_free=usage.free, disk_total=usage.total)
        except OSError:
            item.update(disk_free=None, disk_total=None)
        item.pop("identity")
        roots.append(item)
    jobs = await c.db._all("SELECT id,kind,name,status,state,params,created_at,finished_at FROM jobs WHERE kind LIKE 'maintenance_%' ORDER BY id DESC LIMIT 30", browse=True)

    async def load_groups():
        rows = await c.db._all("""SELECT a.library_id,a.root,
            CASE WHEN instr(replace(a.path,char(92),'/'),'/')>0 THEN substr(replace(a.path,char(92),'/'),1,instr(replace(a.path,char(92),'/'),'/')-1) ELSE a.path END AS directory,
            SUM(a.bytes) AS bytes,SUM(a.extra_bytes) AS extra_bytes,COUNT(*) AS assets
            FROM library_assets a JOIN maintenance_roots r ON r.library_id=a.library_id AND r.root=a.root AND r.scan_id=a.scan_id
            GROUP BY a.library_id,a.root,directory ORDER BY bytes DESC LIMIT 100""", bulk=True)
        return [dict(row) for row in rows]

    # A newly published scan or changed root must not reuse the previous aggregate.
    generation = tuple((row["library_id"], row["root"], row["scan_id"]) for row in published
                       if (row["library_id"], row["root"]) in configured)
    groups = await c.db.stats_cache.get(("maintenance_groups", generation), load_groups, 10)
    return {"roots": roots, "groups": [r for r in groups if (r["library_id"], r["root"]) in configured],
            "archive_root": str(c.store.archive_dir),
            "jobs": [{**dict(r), "state": json.loads(r["state"]), "params": json.loads(r["params"])} for r in jobs]}


@router.post("/maintenance/scan", status_code=202)
async def scan(body: ScanRequest, request: Request):
    async def create():
        try:
            return {"job_id": await _ctx(request).engine.maintenance.create("maintenance_scan", body.model_dump())}
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    return await execute(request, body.model_dump(), create)


@router.post("/maintenance/assets/query")
async def assets(body: AssetFilter, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    try:
        return await list_assets(_ctx(request).db, body, offset, limit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post("/maintenance/preview", status_code=202)
async def preview(body: CleanupRequest, request: Request):
    async def create():
        return {"job_id": await _ctx(request).engine.maintenance.create("maintenance_preview", {"request": body.model_dump()})}
    return await execute(request, body.model_dump(), create)


@router.get("/maintenance/jobs")
async def jobs(request: Request, category: Literal["all", "unavailable"] = "all",
               status: Literal["", "active", "running", "paused", "done", "cancelled"] = "",
               offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    db = _ctx(request).db
    # 执行和恢复任务沿已有引用找到原预览，分类不依赖任务名称。
    tables = """jobs j
        LEFT JOIN jobs p ON p.id=json_extract(j.params,'$.preview_id')
        LEFT JOIN jobs original ON original.id=COALESCE(json_extract(j.params,'$.restore_of'),json_extract(p.params,'$.restore_of'))
        LEFT JOIN jobs original_preview ON original_preview.id=json_extract(original.params,'$.preview_id')"""
    operation = "COALESCE(json_extract(j.params,'$.request'),json_extract(p.params,'$.request'),json_extract(original_preview.params,'$.request'))"
    condition = "j.kind IN ('maintenance_scan','maintenance_preview','maintenance_apply','maintenance_restore_preview','maintenance_restore')"
    args = []
    if category == "unavailable":
        condition += f" AND json_extract({operation},'$.action')='unavailable'"
    if status == "active":
        condition += " AND j.status IN ('running','paused')"
    elif status:
        condition += " AND j.status=?"
        args.append(status)
    total = await db._one(f"SELECT COUNT(*) AS n FROM {tables} WHERE {condition}", args, browse=True)
    rows = await db._all(f"SELECT j.id,j.kind,j.name,j.status,j.state,j.params,j.created_at,j.finished_at,{operation} AS operation_request "
                         f"FROM {tables} WHERE {condition} ORDER BY j.id DESC LIMIT ? OFFSET ?", (*args, limit, offset), browse=True)
    return {"items": [{**dict(r), "state": json.loads(r["state"]), "params": json.loads(r["params"]),
                       "operation_request": json.loads(r["operation_request"]) if r["operation_request"] else None} for r in rows],
            "total": total["n"], "offset": offset, "limit": limit}


@router.get("/maintenance/jobs/{job_id}")
async def details(job_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    try:
        return await _ctx(request).engine.maintenance.items(job_id, offset, limit)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.get("/maintenance/items/{item_id}/files")
async def files(item_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    db = _ctx(request).db
    item = await db._one("SELECT data FROM maintenance_items WHERE id=?", (item_id,))
    if not item:
        raise HTTPException(404, "预览条目不存在")
    original = json.loads(item["data"]).get("original_item", item_id)
    rows = await db._all("SELECT id,source,destination,kind,state,error,json_extract(fingerprint,'$[2]') AS bytes FROM cleanup_files WHERE item_id=? ORDER BY id LIMIT ? OFFSET ?", (original, limit, offset))
    total = (await db._one("SELECT COUNT(*) AS n FROM cleanup_files WHERE item_id=?", (original,)))["n"]
    return {"items": [dict(r) for r in rows], "total": total}


@router.post("/maintenance/jobs/{job_id}/apply", status_code=202)
async def apply(job_id: int, body: Confirm, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.maintenance.approve(job_id, body.preview_token)}
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/maintenance/jobs/{job_id}/restore-preview", status_code=202)
async def restore_preview(job_id: int, request: Request):
    async def create():
        return {"job_id": await _ctx(request).engine.maintenance.create("maintenance_restore_preview", {"restore_of": job_id})}
    return await execute(request, {"restore_of": job_id}, create)


@router.put("/maintenance/libraries/{library_id}/policy")
async def policy(library_id: int, body: PolicyRequest, request: Request):
    c = _ctx(request)
    try:
        c.engine._library(library_id)
        if body.missing_policy == "review":
            await c.engine.maintenance.check_roots([library_id])
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from None
    await c.db._write("UPDATE libraries SET missing_policy=? WHERE id=?", (body.missing_policy, library_id))
    await c.engine.reload_libraries()
    return {"ok": True}


@router.get("/maintenance/exclusions")
async def exclusions(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    rows = await _ctx(request).db._all("""SELECT e.*,v.code,v.slug,v.title,l.name AS library_name,j.status AS job_status
        FROM collection_exclusions e JOIN videos v ON v.id=e.video_id LEFT JOIN libraries l ON l.id=e.library_id
        LEFT JOIN jobs j ON j.id=e.job_id ORDER BY e.created_at DESC,e.video_id LIMIT ? OFFSET ?""", (limit, offset))
    return {"items": [dict(r) for r in rows], "total": (await _ctx(request).db._one("SELECT COUNT(*) AS n FROM collection_exclusions"))["n"]}


@router.delete("/maintenance/exclusions/{video_id}/{library_id}/{job_id}")
async def release(video_id: int, library_id: int, job_id: int, request: Request):
    db = _ctx(request).db
    async with _ctx(request).engine.file_changes.exclusive():
        job = await db.get_job(job_id)
        unfinished = await db._one("SELECT 1 FROM maintenance_items WHERE video_id=? AND preview_id=? AND state IN ('running','failed') LIMIT 1", (video_id, (job or {}).get("params", {}).get("preview_id", -1)))
        if job and (job["status"] in ("running", "paused") or unfinished):
            raise HTTPException(409, "清理尚未完成，请先重试或恢复未完成项")
        await db._write("DELETE FROM collection_exclusions WHERE video_id=? AND library_id=? AND job_id=?", (video_id, library_id, job_id))
    return {"ok": True}


@router.get("/maintenance/videos/{video_id}")
async def local_details(video_id: int, request: Request):
    return await _ctx(request).engine.maintenance.local_details(video_id)


@router.post("/maintenance/jobs/{job_id}/retry", status_code=202)
async def retry(job_id: int, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.maintenance.retry(job_id)}
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/maintenance/jobs/{job_id}/abandon")
async def abandon(job_id: int, request: Request):
    try:
        await _ctx(request).engine.maintenance.abandon(job_id)
        return {"ok": True}
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None


@router.get("/maintenance/resources")
async def resource_policies(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    db = _ctx(request).db
    rows = await db._all("SELECT e.*,v.code,l.name AS library_name FROM resource_exclusions e JOIN videos v ON v.id=e.video_id LEFT JOIN libraries l ON l.id=e.library_id ORDER BY e.job_id DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"items": [dict(r) for r in rows], "total": (await db._one("SELECT COUNT(*) AS n FROM resource_exclusions"))["n"]}


@router.delete("/maintenance/resources/{video_id}/{library_id}/{kind}/{job_id}")
async def release_resource(video_id: int, library_id: int, kind: str, job_id: int, request: Request):
    db = _ctx(request).db
    async with _ctx(request).engine.file_changes.exclusive():
        job = await db.get_job(job_id)
        if job and job["status"] in ("running", "paused"):
            raise HTTPException(409, "清理任务尚未完成")
        await db._write("DELETE FROM resource_exclusions WHERE video_id=? AND library_id=? AND kind=? AND job_id=?", (video_id, library_id, kind, job_id))
    return {"ok": True}
