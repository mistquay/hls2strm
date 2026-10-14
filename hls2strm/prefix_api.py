"""前缀统计及一次性批次 API，认证由应用路由挂载统一提供。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import prefix_crawl, prefix_store
from .request_jobs import execute
from .sites import get_site

router = APIRouter()


class PrefixStats(BaseModel):
    query: dict = Field(default_factory=dict)
    ids: list[int] | None = None
    scope_id: str | None = None
    site: str = "missav"
    background: bool = False


class PrefixBatch(BaseModel):
    scope_id: str
    prefixes: list[str] = Field(min_length=1, max_length=2000)
    sites: list[str] = Field(min_length=1, max_length=6)
    mode: Literal["expand", "existing"] = "expand"
    library_id: int | None = 1
    max_pages: int = Field(0, ge=0, le=10000)
    strict: bool = True
    detail: bool = True


class PrefixCancel(BaseModel):
    site: str
    prefix: str


@router.post("/prefixes/stats")
async def stats(body: PrefixStats, request: Request):
    db = request.app.state.ctx.db
    try:
        get_site(body.site)
        if body.background:
            result = await execute(request, body.model_dump(), lambda: _create_stats(request, body))
            return JSONResponse(result, status_code=202)
        scope_id = body.scope_id or await prefix_store.snapshot(db, body.query, body.ids)
        return await prefix_store.statistics(db, scope_id, body.site)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


async def _create_stats(request, body):
    from .prefix_snapshot import create
    return {"job_id": await create(request.app.state.ctx.engine, body.model_dump())}


async def _prefix_job(request, job_id):
    db = request.app.state.ctx.db
    job = await db.get_job(job_id)
    if not job or job["kind"] != "prefix_crawl":
        raise HTTPException(404, "前缀批次不存在")
    return db


@router.get("/prefixes/jobs/{job_id}/progress")
async def progress(job_id: int, request: Request, q: str = "", limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                   state: Literal["", "unfinished", "queued", "failed"] = ""):
    return await prefix_store.progress_page(await _prefix_job(request, job_id), job_id, q, limit, offset, state)


@router.get("/prefixes/jobs/{job_id}/summary")
async def summary(job_id: int, request: Request):
    db = await _prefix_job(request, job_id)
    return {**await prefix_store.progress_summary(db, job_id), "sequence": await prefix_store.sequence_progress(db, job_id)}


@router.post("/prefixes/jobs")
async def create(body: PrefixBatch, request: Request):
    async def make():
        try:
            job_id = await prefix_crawl.create(request.app.state.ctx.engine, body.model_dump())
            return {"job_id": job_id}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
    return await execute(request, body.model_dump(), make)


@router.post("/prefixes/jobs/{job_id}/cancel")
async def cancel(job_id: int, body: PrefixCancel, request: Request):
    try:
        await prefix_crawl.cancel(request.app.state.ctx.engine, job_id, body.site, body.prefix)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"ok": True}
