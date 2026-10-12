"""已确认的批量变更与普通文件写入互斥；正常写入仍可并发，播放读取不受影响。"""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


class FileChanges:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.ready.set()
        self.drained = asyncio.Event()
        self.drained.set()
        self.active = set()
        self.owner = None

    @asynccontextmanager
    async def normal(self):
        task = asyncio.current_task()
        if task is self.owner or task in self.active:
            yield
            return
        while not self.ready.is_set():
            await self.ready.wait()
        self.active.add(task)
        self.drained.clear()
        try:
            yield
        finally:
            self.active.discard(task)
            if not self.active:
                self.drained.set()

    @asynccontextmanager
    async def exclusive(self):
        async with self.lock:
            self.ready.clear()
            try:
                await self.drained.wait()
                self.owner = asyncio.current_task()
                yield
            finally:
                self.owner = None
                self.ready.set()


def guarded(fn):
    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        async with self.file_changes.normal():
            return await fn(self, *args, **kwargs)
    return wrapped
