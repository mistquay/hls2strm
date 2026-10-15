"""扫描各阶段共享进度；耗时文件读取放在线程里，定时发布当前位置。"""

import asyncio
from collections import Counter
import time


class ScanProgress:
    def __init__(self, engine, job_id):
        self.engine, self.job_id = engine, job_id
        self.counts = Counter()
        self.state = {}
        self.activity = {}
        self.last_update = 0
        self.started = time.monotonic()

    def visiting(self, path):
        # 由文件读取线程更新，事件循环只读取快照。
        self.activity["current_path"] = str(path)

    async def publish(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_update < 1:
            return
        await self.engine.checkpoint(self.job_id)
        await self.engine.db.update_job(self.job_id, state={
            **dict(self.counts), **self.state, **self.activity,
            "elapsed_seconds": int(now - self.started), "updated_at": int(time.time()),
        })
        self.last_update = now

    async def phase(self, name, **fields):
        self.state.update(scan_phase=name, **fields)
        self.activity.clear()
        await self.publish(force=True)

    async def wait(self, operation):
        worker = asyncio.ensure_future(operation)
        try:
            while not worker.done():
                await asyncio.wait({worker}, timeout=1)
                await self.publish()
            return await worker
        finally:
            # 暂停/取消也先收回线程，避免关闭仍在 next() 的目录生成器。
            if not worker.done():
                await asyncio.shield(worker)
