"""取消当前待处理的抓取子任务，保留执行结果及流程步骤。"""

import logging
import time
from typing import Literal, get_args

CancellableKind = Literal["list", "detail", "probe", "cover", "quality"]
CANCELLABLE_KINDS = get_args(CancellableKind)
log = logging.getLogger(__name__)


async def cancel_pending(engine, job_id, *, task_ids=None, site=None, kind=None):
    conditions = ["job_id=?", "status='pending'", f"kind IN ({','.join('?' * len(CANCELLABLE_KINDS))})"]
    params = [job_id, *CANCELLABLE_KINDS]
    if task_ids is not None:
        if not task_ids:
            return 0
        conditions.append(f"id IN ({','.join('?' * len(task_ids))})")
        params.extend(task_ids)
    if site is not None:
        conditions.append("site=?")
        params.append(site)
    if kind is not None:
        conditions.append("kind=?")
        params.append(kind)
    async with engine._claim_lock:
        async with engine.db._tx(invalidate=False) as conn:
            job = await engine.db.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in ("running", "paused"):
                return 0
            cur = await conn.execute(
                "UPDATE tasks SET status='cancelled',last_error='用户取消待处理子任务',next_run_at=0,updated_at=? WHERE "
                + " AND ".join(conditions), (int(time.time()), *params))
            count = cur.rowcount
        if count:
            await engine._update_list_completion(job_id)
            await engine._maybe_finish_job(job_id, allow_paused=True)
    if count:
        log.info("任务 #%d：用户取消 %d 个待处理子任务（站点 %s，类型 %s）", job_id, count,
                 site if site is not None else "全部", kind or "全部抓取类型")
        engine.notify()
    return count
