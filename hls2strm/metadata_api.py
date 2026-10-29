"""Authenticated manual metadata preview and execution endpoints."""

import json

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .api import _ctx
from .metadata_fallback import RepairRequest
from .request_jobs import execute

router = APIRouter()


class Confirmation(BaseModel):
    preview_token: str
    scraper_paused: bool = False


@router.post("/metadata/preview", status_code=202)
async def preview(body: RepairRequest, request: Request):
    async def create():
        try:
            return {"job_id": await _ctx(request).engine.metadata.create(body)}
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    return await execute(request, body.model_dump(), create)


@router.get("/metadata/jobs")
async def jobs(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    db = _ctx(request).db
    rows = await db._all("SELECT id FROM jobs WHERE kind IN ('metadata_preview','metadata_apply') ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    total = await db._one("SELECT COUNT(*) AS n FROM jobs WHERE kind IN ('metadata_preview','metadata_apply')")
    return {"items": [await db.get_job(row["id"]) for row in rows], "total": total["n"]}


@router.get("/metadata/jobs/{job_id}")
async def details(job_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    db = _ctx(request).db
    job = await db.get_job(job_id)
    if not job or job["kind"] not in ("metadata_preview", "metadata_apply"):
        raise HTTPException(404, "补全任务不存在")
    pid = job["params"].get("preview_id", job_id)
    preview = await db.get_job(pid)
    rows = await db._all("SELECT * FROM maintenance_items WHERE preview_id=? ORDER BY id LIMIT ? OFFSET ?", (pid, limit, offset))
    items = []
    for row in rows:
        data = json.loads(row["data"])
        items.append({"id": row["id"], "state": row["state"], "error": row["error"],
                      **{key: data.get(key) for key in ("code", "title", "source", "target", "changes", "files", "warnings")}})
    total = await db._one("SELECT COUNT(*) AS n FROM maintenance_items WHERE preview_id=?", (pid,))
    return {"job": job, "request": preview["params"], "items": items, "total": total["n"]}


@router.post("/metadata/jobs/{job_id}/apply", status_code=202)
async def apply(job_id: int, body: Confirmation, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.metadata.approve(job_id, body.preview_token, body.scraper_paused)}
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/metadata/jobs/{job_id}/retry", status_code=202)
async def retry(job_id: int, body: Confirmation, request: Request):
    if not body.scraper_paused:
        raise HTTPException(409, "重试前请暂停对应外部刮削写入")
    try:
        await _ctx(request).engine.metadata.retry(job_id)
        return {"job_id": job_id}
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
