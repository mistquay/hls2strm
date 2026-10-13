"""前缀统计及一次性批次 API，认证由应用路由挂载统一提供。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
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
        scope_id = body.scope_id or await prefix_store.snapshot(db, body.query, body.ids)
        return await prefix_store.statistics(db, scope_id, body.site)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


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
