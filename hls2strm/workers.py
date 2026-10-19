"""Worker supervision outside business-task retry handling."""

import asyncio
import logging
import time

log = logging.getLogger(__name__)
RETRY_DELAY = 1


async def run_worker(engine, index):
    recovery, failures = None, 0
    while not engine._stopping:
        task = None
        try:
            if recovery is not None:
                item, requeue = recovery
                # A log/finalization error must not replay completed side effects.
                if requeue:
                    await engine.db._write("""UPDATE tasks SET status='pending',attempts=MAX(0,attempts-1),
                        last_error='worker 异常中断，已重新排队',updated_at=? WHERE id=? AND status='running'""",
                        (int(time.time()), item["id"]), invalidate=False)
                    recovery = (item, False)
                if item["kind"] == "list":
                    await engine._update_list_completion(item["job_id"])
                await engine._maybe_finish_job(item["job_id"])
                recovery = None
            if index >= engine._worker_count():
                return
            if engine.paused:
                await engine._idle(2)
                continue
            async with engine._claim_lock:
                if engine.paused:
                    continue
                task = await engine.db.claim_task(engine._skip_sites())
                if task is not None:
                    if engine.paused:
                        await engine.db.finish_task(task["id"], "pending", refund_attempt=True)
                        continue
                    engine.running[task["id"]] = {
                        "id": task["id"], "job_id": task["job_id"], "kind": task["kind"], "site": task["site"],
                        "target": task["target"], "attempt": task["attempts"], "started": time.time()}
            if task is None:
                await engine._idle(2)
            else:
                await engine._run(task)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            if task is not None:
                engine.running.pop(task["id"], None)
                recovery = (task, True)
            engine.metrics.inc("worker_errors")
            failures += 1
            delay = min(30, RETRY_DELAY * 2 ** min(failures - 1, 5))
            log.exception("worker-%d 外层执行异常，%s 秒后重试", index, delay)
            await asyncio.sleep(delay)
