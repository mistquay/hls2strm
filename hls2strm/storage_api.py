"""目录统计、固定清理预览与确认接口。"""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .api import _ctx
from .request_jobs import execute

router = APIRouter()


class ScanBody(BaseModel):
    keep: int = Field(3, ge=0, le=10000)


class CleanBody(BaseModel):
    preview_id: int
    preview_token: str = Field(min_length=1)


class TidyConfirmation(BaseModel):
    preview_token: str = Field(min_length=1)


@router.get("/tidy/{job_id}")
async def tidy_result(job_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    from .tidy_plans import result
    try:
        return await result(_ctx(request).engine.strm, job_id, offset, limit)
    except (OSError, ValueError) as error:
        raise HTTPException(404, str(error)) from None


@router.post("/tidy/{job_id}/apply", status_code=202)
async def tidy_apply(job_id: int, body: TidyConfirmation, request: Request):
    from .tidy_plans import approve
    try:
        engine = _ctx(request).engine
        applied = await approve(engine.strm, job_id, body.preview_token)
        await engine.scheduler.track_job("system:orphans", applied)
        return {"job_id": applied}
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None


@router.get("/libraries/{library_id}/storage")
async def latest(library_id: int, request: Request):
    row = await _ctx(request).db._one(
        "SELECT id FROM jobs WHERE kind='storage_scan' AND json_extract(params,'$.library_id')=? ORDER BY id DESC LIMIT 1",
        (library_id,))
    return {"job_id": row["id"] if row else None}


@router.post("/libraries/{library_id}/storage", status_code=202)
async def scan(library_id: int, body: ScanBody, request: Request):
    async def create():
        try:
            return {"job_id": await _ctx(request).engine.storage.create(library_id, body.keep)}
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from None
    return await execute(request, body.model_dump(), create)


@router.get("/libraries/{library_id}/storage/{job_id}")
async def result(library_id: int, job_id: int, request: Request):
    job = await _ctx(request).db.get_job(job_id)
    if not job or job["kind"] not in ("storage_scan", "storage_clean") or job["params"]["library_id"] != library_id:
        raise HTTPException(404, "目录任务不存在")
    return {"id": job_id, "status": job["status"], "state": job["state"]}


@router.get("/libraries/{library_id}/storage/{job_id}/files")
async def storage_files(library_id: int, job_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    job = await _ctx(request).db.get_job(job_id)
    if not job or job["kind"] not in ("storage_scan", "storage_clean") or job["params"]["library_id"] != library_id:
        raise HTTPException(404, "目录任务不存在")
    storage = _ctx(request).engine.storage
    source_id = job["params"].get("preview_id", job_id)
    source = await _ctx(request).db.get_job(source_id)
    path = storage.manifest(source_id)
    if not source or not path.is_file():
        return {"items": [], "total": 0, "available": False}

    def read():
        items, cursor = [], 0
        total = source["state"].get("delete_files", source["state"].get("summary", {}).get("counts", {}).get("delete_files", 0))
        with path.open(encoding="utf-8") as manifest:
            for line in manifest:
                for relative, fingerprint in json.loads(line)["files"]:
                    if offset <= cursor < offset + limit:
                        items.append({"path": str(Path(source["params"]["root"]) / relative), "bytes": fingerprint[2]})
                    cursor += 1
                    if cursor >= offset + limit:
                        return {"items": items, "total": total, "available": True}
        return {"items": items, "total": total, "available": True}
    return await asyncio.to_thread(read)


@router.post("/libraries/{library_id}/storage/clean", status_code=202)
async def clean(library_id: int, body: CleanBody, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.storage.approve(library_id, body.preview_id, body.preview_token)}
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None
