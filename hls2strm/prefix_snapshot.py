"""在稳定读取快照上分批构建前缀范围；完成前不向抓取任务发布。"""

import asyncio
import hashlib
import json
import time
import uuid

import aiosqlite

from .codes import series_prefix
from .operation_plans import selection_query

BATCH_SIZE = 256


def validate(query, ids):
    condition, params = selection_query({"query": query}).where()
    if ids is not None:
        if not ids or any(type(value) is not int or value < 1 for value in ids):
            raise ValueError("显式选择必须是非空的影片 ID 列表")
        condition, params = "id IN (SELECT value FROM json_each(?))", [json.dumps(sorted(set(ids)))]
    return condition, params


async def _noop(*args):
    pass


async def _clear_rows(db, scope_id, checkpoint):
    while True:
        await checkpoint()
        cur = await db._write(
            "DELETE FROM prefix_scope_videos WHERE rowid IN "
            "(SELECT rowid FROM prefix_scope_videos WHERE scope_id=? LIMIT ?)",
            (scope_id, BATCH_SIZE), invalidate=False)
        if not cur.rowcount:
            break
        await asyncio.sleep(0)


async def build(db, query, ids=None, *, scope_id=None, checkpoint=_noop, on_batch=_noop):
    condition, params = validate(query, ids)
    scope_id = scope_id or uuid.uuid4().hex
    # 每个进程最多一个快照构建连接；普通统计查询仍使用批量读取连接。
    async with db.snapshot_lock:
        await checkpoint()
        scope = await db._one("SELECT ready FROM prefix_scopes WHERE id=?", (scope_id,))
        if scope and scope["ready"]:
            return scope_id
        expired = await db._all(
            """SELECT id FROM prefix_scopes WHERE created_at<? AND NOT EXISTS
               (SELECT 1 FROM jobs WHERE json_extract(params,'$.scope_id')=prefix_scopes.id)
               LIMIT 100""", (int(time.time()) - 86400,), bulk=True)
        for old in expired:
            await _clear_rows(db, old["id"], checkpoint)
            await db._write("DELETE FROM prefix_scopes WHERE id=?", (old["id"],), invalidate=False)
        await db._write("INSERT OR IGNORE INTO prefix_scopes(id,created_at,ready) VALUES(?,?,0)",
                        (scope_id, int(time.time())), invalidate=False)
        # 重启/暂停后的未发布范围重新构建；已发布的范围始终原样复用。
        await _clear_rows(db, scope_id, checkpoint)
        async with aiosqlite.connect(db.path, isolation_level=None) as reader:
            reader.row_factory = aiosqlite.Row
            await reader.execute("PRAGMA query_only=ON")
            await reader.execute("BEGIN")
            try:
                async with reader.execute(f"SELECT COUNT(*) AS n FROM videos WHERE {condition}", params) as cur:
                    total = (await cur.fetchone())["n"]
                fingerprint, processed = hashlib.sha256(), 0
                await on_batch(scope_id, processed, total)
                async with reader.execute(f"SELECT id,code_key,code FROM videos WHERE {condition} ORDER BY id", params) as cur:
                    while rows := await cur.fetchmany(BATCH_SIZE):
                        await checkpoint()
                        values = [(scope_id, row["id"], series_prefix(row["code_key"]), row["code"]) for row in rows]
                        for row in rows:
                            fingerprint.update(str(row["id"]).encode() + b",")
                        async with db._tx(invalidate=False) as conn:
                            await checkpoint()
                            await conn.executemany("INSERT INTO prefix_scope_videos(scope_id,video_id,prefix,code) VALUES(?,?,?,?)", values)
                        processed += len(rows)
                        await on_batch(scope_id, processed, total)
                        await asyncio.sleep(0)
                # ready 与 fingerprint 一次发布；取消与发布在共享写锁内复核。
                async with db._tx(invalidate=False) as conn:
                    await checkpoint()
                    await conn.execute("UPDATE prefix_scopes SET fingerprint=?,ready=1 WHERE id=?", (fingerprint.hexdigest(), scope_id))
            finally:
                await reader.execute("ROLLBACK")
    return scope_id


async def create(engine, body):
    validate(body["query"], body["ids"])
    if body.get("scope_id") and not await engine.db._one(
            "SELECT 1 FROM prefix_scopes WHERE id=? AND ready=1", (body["scope_id"],)):
        raise ValueError("统计快照已过期或尚未完成，请重新统计")
    params = {key: value for key, value in body.items() if key != "background"}
    params["scope_id"] = body.get("scope_id") or uuid.uuid4().hex
    job_id = await engine.db.create_job("prefix_stats", "统计影片前缀", params)
    await engine.db.add_tasks(job_id, "prefix_stats", ["all"], 20)
    engine.notify()
    return job_id


async def run(engine, job, task):
    from .prefix_store import statistics
    db, params = engine.db, job["params"]
    state, last_update = dict(job["state"]), 0.0

    async def checkpoint():
        await engine.checkpoint(job["id"])

    async def on_batch(scope_id, processed, total):
        nonlocal last_update
        state.update(scope_id=scope_id, processed=processed, total=total)
        now = time.monotonic()
        if processed == total or now - last_update >= .5:
            await db.update_job(job["id"], state=state)
            last_update = now

    await build(db, params["query"], params["ids"], scope_id=params["scope_id"], checkpoint=checkpoint, on_batch=on_batch)
    await checkpoint()
    result = await statistics(db, params["scope_id"], params["site"])
    state.update(scope_id=params["scope_id"], total=result["total"], processed=result["total"], site=params["site"])
    await db.update_job(job["id"], state=state)
    return {"processed": result["total"]}
