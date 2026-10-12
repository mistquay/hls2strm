"""系统计划与调度执行记录；订阅计划仍由 subscriptions 保存。"""

import json
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemTask:
    name: str
    interval: int
    startup: bool = True
    follows_pause: bool = True
    destructive: bool = False
    note: str = ""


SYSTEM_TASKS = {
    "external_locate": SystemTask("外部整理库同步位置", 10, note="按内容找回移动、改名后的 STRM；开启多画质版本时补齐版本文件"),
    "health": SystemTask("播放连通性检测", 60, startup=False, note="抽样检测最近播放的源，启动后等待一个周期"),
    "settle": SystemTask("来源库与排除库归并兜底", 10, note="定时兜底之外仍按事件归并；排除库等待条件不变"),
    "trash": SystemTask("回收区到期清理", 60, follows_pause=False, destructive=True, note="永久删除放满保留天数的回收区内容；不随业务暂停"),
    "play_history": SystemTask("播放记录清理", 60, follows_pause=False, destructive=True, note="按保留天数和最大条数清理；不随业务暂停"),
    "orphans": SystemTask("外部整理库残留清理", 1440, destructive=True, note="仅清理满足原有保护条件的残留目录，按保留策略处理"),
}


async def migrate_v21(conn):
    await conn.execute("""CREATE TABLE system_schedules (
        key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
        cron TEXT, interval INTEGER NOT NULL, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        schedule_updated_at INTEGER NOT NULL, last_run_at INTEGER NOT NULL DEFAULT 0)""")
    await conn.execute("""CREATE TABLE schedule_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_key TEXT NOT NULL,
        trigger TEXT NOT NULL, planned_at INTEGER, dispatched_at INTEGER NOT NULL,
        started_at INTEGER, finished_at INTEGER, status TEXT NOT NULL,
        job_id INTEGER, request_key TEXT UNIQUE, result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '')""")
    await conn.execute("CREATE INDEX schedule_runs_key ON schedule_runs(schedule_key,id DESC)")
    await conn.execute("CREATE INDEX schedule_runs_active ON schedule_runs(status,id)")
    await conn.execute("CREATE UNIQUE INDEX schedule_runs_job ON schedule_runs(job_id) WHERE job_id IS NOT NULL")
    async with conn.execute("SELECT value FROM settings WHERE key='settings'") as cur:
        row = await cur.fetchone()
    settings = json.loads(row[0]) if row else {}
    for key, definition in SYSTEM_TASKS.items():
        interval = settings.get({"external_locate": "external_sync_interval", "health": "health_interval"}.get(key), definition.interval)
        enabled = interval > 0 and (key != "orphans" or settings.get("orphan_cleanup", True))
        await conn.execute("INSERT INTO system_schedules(key,enabled,interval,schedule_updated_at) VALUES(?,?,?,?)",
                           (key, int(enabled), interval or definition.interval, 0))


class ScheduleStore:
    def __init__(self, db):
        self.db = db

    async def systems(self):
        return [dict(row) for row in await self.db._all("SELECT * FROM system_schedules ORDER BY rowid")]

    async def system(self, key):
        row = await self.db._one("SELECT * FROM system_schedules WHERE key=?", (key,))
        if row is None:
            raise KeyError(key)
        return dict(row)

    async def update(self, key, fields):
        await self.db._write("UPDATE system_schedules SET enabled=?,cron=?,interval=?,timezone=?,schedule_updated_at=? WHERE key=?",
                             (int(fields["enabled"]), fields["cron"], fields["interval"], fields["timezone"], int(time.time()), key), invalidate=False)

    async def request(self, key):
        row = await self.db._one("SELECT * FROM schedule_runs WHERE request_key=?", (key,))
        return dict(row) if row else None

    async def begin(self, key, trigger, planned_at, request_key=None):
        cur = await self.db._write("INSERT INTO schedule_runs(schedule_key,trigger,planned_at,dispatched_at,request_key,status) VALUES(?,?,?,?,?, 'queued')",
                                  (key, trigger, planned_at, int(time.time()), request_key), invalidate=False)
        return cur.lastrowid

    async def attach(self, run_id, job_id):
        await self.db._write("UPDATE schedule_runs SET job_id=? WHERE id=?", (job_id, run_id), invalidate=False)

    async def started(self, run_id):
        await self.db._write("UPDATE schedule_runs SET status='running',started_at=? WHERE id=?", (int(time.time()), run_id), invalidate=False)

    async def finish(self, run_id, status, result=None, error=""):
        await self.db._write("UPDATE schedule_runs SET status=?,finished_at=?,result=?,error=? WHERE id=?",
                             (status, int(time.time()), json.dumps(result or {}, ensure_ascii=False), error[:2000], run_id), invalidate=False)

    async def recover(self):
        await self.db._write("UPDATE schedule_runs SET status='interrupted',finished_at=?,error='服务退出，执行未完成' WHERE job_id IS NULL AND status IN ('queued','running')",
                             (int(time.time()),), invalidate=False)

    async def reconcile(self, job_id=None):
        # 一次联表更新，无需对每个订阅或历史任务单独查询。
        await self.db._write("""UPDATE schedule_runs SET
            status=(SELECT CASE WHEN j.status='done' AND (COALESCE(json_extract(j.state,'$.partial_failure'),0)=1 OR COALESCE(json_extract(j.state,'$.failed'),0)>0)
                           THEN 'failed' WHEN j.status='running' AND j.started_at IS NULL THEN 'queued' ELSE j.status END
                    FROM jobs j WHERE j.id=schedule_runs.job_id),
            started_at=(SELECT started_at FROM jobs WHERE id=schedule_runs.job_id),
            finished_at=(SELECT finished_at FROM jobs WHERE id=schedule_runs.job_id),
            result=(SELECT state FROM jobs WHERE id=schedule_runs.job_id)
            WHERE job_id IS NOT NULL AND EXISTS(SELECT 1 FROM jobs WHERE id=job_id)
              AND """ + ("job_id=?" if job_id is not None else "status IN ('queued','running','paused')"),
            (job_id,) if job_id is not None else (), invalidate=False)

    async def latest(self):
        rows = await self.db._all("""SELECT r.* FROM
            (SELECT 'system:'||key AS schedule_key FROM system_schedules
             UNION ALL SELECT 'subscription:'||id FROM subscriptions) plans
            JOIN schedule_runs r ON r.id=(SELECT id FROM schedule_runs
                WHERE schedule_key=plans.schedule_key ORDER BY id DESC LIMIT 1)""", bulk=True)
        rows = await self._with_jobs(rows)
        return {r["schedule_key"]: self.decode(r) for r in rows}

    async def history(self, key, limit, offset):
        total = (await self.db._one("SELECT COUNT(*) AS n FROM schedule_runs WHERE schedule_key=?", (key,), bulk=True))["n"]
        rows = await self.db._all("SELECT * FROM schedule_runs WHERE schedule_key=? ORDER BY id DESC LIMIT ? OFFSET ?", (key, limit, offset), bulk=True)
        return {"items": [self.decode(r) for r in await self._with_jobs(rows)], "total": total}

    async def _with_jobs(self, rows):
        ids = {r["job_id"] for r in rows if r["job_id"]}
        jobs = {}
        if ids:
            jobs = {j["id"]: dict(j) for j in await self.db._all(
                "SELECT id,status,started_at,finished_at,state FROM jobs WHERE id IN (" + ",".join("?" for _ in ids) + ")", list(ids), bulk=True)}
        result = []
        for row in rows:
            row = dict(row)
            job = jobs.get(row["job_id"])
            if job:
                state = json.loads(job["state"])
                status = job["status"]
                if status == "done" and (state.get("partial_failure") or state.get("failed")):
                    status = "failed"
                if status == "running" and not job["started_at"]:
                    status = "queued"
                row.update(status=status, started_at=job["started_at"], finished_at=job["finished_at"], result=job["state"])
            result.append(row)
        return result

    @staticmethod
    def decode(row):
        return {**dict(row), "result": json.loads(row["result"])}


async def legacy_settings(conn, patch):
    """兼容旧设置 API；实际调度始终读取 system_schedules。"""
    for setting, key in (("external_sync_interval", "external_locate"), ("health_interval", "health")):
        if setting in patch:
            interval = patch[setting]
            await conn.execute("UPDATE system_schedules SET cron=NULL,interval=?,enabled=?,schedule_updated_at=? WHERE key=?",
                               (interval or SYSTEM_TASKS[key].interval, int(interval > 0), int(time.time()), key))
    if "orphan_cleanup" in patch:
        await conn.execute("UPDATE system_schedules SET enabled=?,schedule_updated_at=? WHERE key='orphans'",
                           (int(patch["orphan_cleanup"]), int(time.time())))
