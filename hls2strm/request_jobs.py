"""同一次提交的持久化任务回执；有意再次提交使用新的请求键。"""

from contextvars import ContextVar
from fastapi import HTTPException

from .operation_plans import digest

current_request = ContextVar("job_request",default=None)


async def execute(request,body,create,field="job_id"):
    key = request.headers.get("Idempotency-Key","")
    if not key:
        return await create()
    if len(key)>128:
        raise HTTPException(422,"提交标识过长")
    engine = request.app.state.ctx.engine
    fingerprint = digest([request.url.path,body])
    async with engine.request_lock:
        row = await engine.db._one("SELECT * FROM operation_requests WHERE key=?",(key,))
        if row and row["digest"]!=fingerprint:
            raise HTTPException(409,"同一提交标识不能用于不同操作")
        if row and row["ready"]:
            return {field:row["job_id"]}
        token = current_request.set((key,fingerprint))
        try:
            result = await create()
            await engine.db._write("UPDATE operation_requests SET ready=1 WHERE key=?",(key,))
            await engine._maybe_finish_job(result[field])
            engine.notify()
            return result
        finally:
            current_request.reset(token)
