"""统一计划管理 API，挂在已鉴权的 /api 路由下。"""

import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .schedule import ScheduleFields, preview
from .schedule_store import SYSTEM_TASKS

router = APIRouter(prefix="/schedules")


class ScheduleUpdate(ScheduleFields):
    model_config = ConfigDict(extra="forbid")
    cron: str | None
    interval: int = Field(ge=0)
    enabled: bool = True


class RunBody(BaseModel):
    confirm: bool = False


def scheduler(request):
    return request.app.state.ctx.engine.scheduler


@router.get("")
async def list_schedules(request: Request):
    return await scheduler(request).list()


@router.post("/preview")
async def preview_schedule(body: ScheduleFields):
    times = preview(body, time.time())
    return {"times": times, "next_run_at": times[0] if times else None, "timezone": body.timezone}


@router.put("/{key}")
async def update_schedule(key: str, body: ScheduleUpdate, request: Request):
    try:
        return await scheduler(request).update(key, body.model_dump())
    except KeyError:
        raise HTTPException(404, "定时任务不存在") from None


@router.post("/{key}/run")
async def run_schedule(key: str, body: RunBody, request: Request):
    try:
        plan = await scheduler(request).plan(key)
        if plan["kind"] == "system" and SYSTEM_TASKS[key.split(":", 1)[1]].destructive and not body.confirm:
            raise HTTPException(400, "此任务会清理文件或记录，请确认后执行")
        return await scheduler(request).run(key, request_key=request.headers.get("Idempotency-Key"))
    except KeyError:
        raise HTTPException(404, "定时任务不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{key}/history")
async def schedule_history(key: str, request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    try:
        await scheduler(request).plan(key)
    except KeyError:
        raise HTTPException(404, "定时任务不存在") from None
    return await scheduler(request).store.history(key, limit, offset)
