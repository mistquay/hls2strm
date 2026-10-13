"""操作预览与确认接口；计算和范围复核均在可取消、可恢复的任务中执行。"""

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .api import _ctx, LibraryBody
from .job_views import details

router = APIRouter()


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["library","retirement","selection","dedupe"]
    payload: dict


class ConfirmOperation(BaseModel):
    preview_token: str = Field(min_length=1)


@router.post("/operations/preview",status_code=202)
async def preview_operation(body:OperationRequest,request:Request):
    from .request_jobs import execute
    return await execute(request,body.model_dump(),lambda:_preview_operation(body,request))


async def _preview_operation(body:OperationRequest,request:Request):
    value = body.model_dump()
    try:
        if body.kind=="library":
            value["payload"]["values"] = LibraryBody.model_validate(body.payload["values"]).model_dump(exclude={"preview_id","preview_token"})
        job = await _ctx(request).engine.operations.create(value)
        return {"job_id":job}
    except (ValueError,KeyError,ValidationError) as error:
        raise HTTPException(422,str(error)) from None


@router.get("/operations/{job_id}")
async def operation_preview(job_id:int,request:Request):
    c = _ctx(request)
    job = await c.db.get_job(job_id)
    if not job or job["kind"]!="preview":
        raise HTTPException(404,"预览任务不存在")
    result = job["state"].get("_preview")
    token = job["state"].get("preview_token")
    return {"job":await details(c.engine,job),"preview":result,"preview_token":token}


@router.post("/operations/{job_id}/apply",status_code=202)
async def apply_operation(job_id:int,body:ConfirmOperation,request:Request):
    try:
        return {"job_id":await _ctx(request).engine.operations.approve(job_id,body.preview_token)}
    except ValueError as error:
        raise HTTPException(409,str(error)) from None


@router.get("/operations/{job_id}/groups")
async def operation_groups(job_id: int, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    db = _ctx(request).db
    job = await db.get_job(job_id)
    if not job or job["kind"] != "preview" or job["params"].get("operation", {}).get("kind") != "dedupe":
        raise HTTPException(404, "同片整理预览不存在")
    rows = await db._all("SELECT data FROM operation_items WHERE preview_id=? ORDER BY video_id LIMIT ? OFFSET ?", (job_id, limit, offset), bulk=True)
    total = (await db._one("SELECT COUNT(*) AS n FROM operation_items WHERE preview_id=?", (job_id,), bulk=True))["n"]
    return {"items": [json.loads(r["data"]) for r in rows], "total": total}
