"""操作预览与确认接口；计算和范围复核均在可取消、可恢复的任务中执行。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .api import _ctx, LibraryBody
from .job_views import details

router = APIRouter()


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["library","retirement","selection"]
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
