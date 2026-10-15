"""目录统计、固定清理预览与确认接口。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .api import _ctx
from .request_jobs import execute

router = APIRouter()


class ScanBody(BaseModel):
    keep: int = Field(3, ge=0, le=10000)


class CleanBody(BaseModel):
    preview_id: int
    preview_token: str = Field(min_length=1)


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


@router.post("/libraries/{library_id}/storage/clean", status_code=202)
async def clean(library_id: int, body: CleanBody, request: Request):
    try:
        return {"job_id": await _ctx(request).engine.storage.approve(library_id, body.preview_id, body.preview_token)}
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None
