"""设置与既有输出的同步记录；重写成功且版本匹配才算同步完成。"""

import json
import secrets
import time

KEYS = {"public_base_url", "play_mode", "play_token", "path_template", "output_dir", "write_nfo"}


async def changed(db, old, new):
    keys = sorted(key for key in KEYS if getattr(old, key) != getattr(new, key))
    if not keys:
        return None
    previous = await view(db)
    if previous and previous["status"] != "synced":
        keys = sorted(set(keys) | set(previous.get("keys", [])))
    return {"revision": secrets.token_hex(16), "keys": keys, "job_id": None, "updated_at": int(time.time())}


async def view(db):
    raw = await db.get_setting("output_sync")
    if not raw:
        return None
    state = json.loads(raw)
    state["status"] = state.get("status", "pending")
    if state.get("job_id"):
        job = await db.get_job(state["job_id"])
        if job and job["params"].get("sync_revision") == state["revision"]:
            if job["status"] == "done":
                failed = await db._one("SELECT 1 FROM tasks WHERE job_id=? AND status IN ('failed','cancelled') LIMIT 1",(job["id"],),bulk=True)
                state["status"] = "partial_failure" if failed or job["state"].get("partial_failure") else "synced"
            else:
                state["status"] = job["status"]
    return state


async def finish(db, job):
    if not job["params"].get("sync_revision"):
        return
    async with db._tx(invalidate=False) as conn:
        state = await view(db)
        if state and state.get("job_id") == job["id"] and state["revision"] == job["params"]["sync_revision"]:
            await conn.execute("UPDATE settings SET value=? WHERE key='output_sync'", (json.dumps(state),))


async def attach(db, revision, job_id):
    async with db._tx(invalidate=False) as conn:
        raw = await db.get_setting("output_sync")
        if raw:
            state = json.loads(raw)
            if state["revision"] == revision:
                state["job_id"] = job_id
                await conn.execute("UPDATE settings SET value=? WHERE key='output_sync'", (json.dumps(state),))
