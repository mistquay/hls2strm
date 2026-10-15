"""统一发现、调度与管理订阅和系统维护任务。"""

import asyncio
import logging
import time

from .schedule import after, has_schedule
from .schedule_store import SYSTEM_TASKS, ScheduleStore
from .subscription_schedule import next_run_at, schedule_anchor, scheduled_after
from .trash import purge as purge_trash

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine):
        self.e = engine
        self.db = engine.db
        self.store = ScheduleStore(self.db)
        self.started_at = engine._subscription_schedule_started_at
        self._startup = {key for key, spec in SYSTEM_TASKS.items() if spec.startup}
        self._tasks = {}
        self._lock = asyncio.Lock()

    async def start(self):
        await self.store.recover()
        await self.store.reconcile()

    async def stop(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _plans(self):
        systems = [{**p, "key": "system:" + p["key"], "kind": "system"} for p in await self.store.systems()]
        subs = [{**s, "key": f"subscription:{s['id']}", "kind": "subscription"} for s in await self.db.list_subscriptions()]
        return systems + subs

    async def plan(self, key):
        prefix, _, identifier = key.partition(":")
        if prefix == "system" and identifier in SYSTEM_TASKS:
            return {**await self.store.system(identifier), "key": key, "kind": "system"}
        if prefix == "subscription" and identifier.isdigit():
            sub = await self.db.get_subscription(int(identifier))
            if sub:
                return {**sub, "key": key, "kind": "subscription"}
        raise KeyError(key)

    async def _context(self):
        jobs = await self.db._all("SELECT id,kind,status,started_at FROM jobs WHERE status IN ('running','paused') AND kind IN ('locate','tidy') ORDER BY id DESC", bulk=True)
        return {kind: next((dict(j) for j in jobs if j["kind"] == kind), None) for kind in ("locate", "tidy")}

    async def _reason(self, plan, context, *, manual=False, event=False):
        if plan["kind"] == "subscription":
            active = (plan.get("listing_job_id") if "listing_job_id" in plan
                      else await self.db.subscription_active_job(plan["id"], listing_only=True))
            if active:
                return "active", "已有列表任务，等待完成"
        else:
            identifier = plan["key"].split(":", 1)[1]
            if identifier in self._tasks or (identifier == "health" and self.e._health_task and not self.e._health_task.done()):
                return "running", "执行中"
            job = context.get({"external_locate": "locate", "orphans": "tidy"}.get(identifier))
            if job:
                state = "paused" if job["status"] == "paused" else "running" if job["started_at"] else "queued"
                return state, f"已有任务 #{job['id']}"
        if not manual and not event and not plan["enabled"]:
            return "disabled", "已停用"
        if not manual and not event and not has_schedule(plan):
            return "manual", "仅手动运行"
        if plan["kind"] == "subscription":
            if self.e.paused:
                return "paused", "业务调度已暂停"
            if not self.e.store.current.site(plan["site"]).enabled:
                return "site_off", "站点已停用"
            if not manual and not plan["initialized"]:
                return "uninitialized", "等待首轮全量"
            lib = self.e.libs.get(plan["library_id"])
            if not lib:
                return "no_targets", "输出库不存在"
            if lib.get("operation_pending"):
                return "library_pending", "输出库变更尚未完成"
            return None
        key = plan["key"].split(":", 1)[1]
        if self.e.paused and SYSTEM_TASKS[key].follows_pause:
            return "paused", "业务调度已暂停"
        if key in ("external_locate", "orphans", "trash"):
            if key == "trash" and await self.db._one("SELECT 1 FROM cleanup_files WHERE state='trashed' LIMIT 1"):
                return None  # 普通库的维护回收文件同样按保留期限处理。
            external = [lib for lib in self.e.libs.values() if lib["external_dir"]]
            if not external:
                return "no_targets", "没有外部整理库"
            if not any(not lib.get("operation_pending") for lib in external):
                return "library_pending", "外部整理库变更尚未完成"
        if key == "play_history" and not (self.e.store.current.play_history_days or self.e.store.current.play_history_max_entries):
            return "no_targets", "未设置播放记录保留限制"
        if key == "settle" and not any(lib["sources"] or lib["excludes"] for lib in self.e.libs.values()):
            if not await self.db.libraries_with_source_outputs():
                return "no_targets", "没有需要归并的库"
        return None

    async def next_at(self, plan):
        if not plan["enabled"] or not has_schedule(plan):
            return None
        if plan["kind"] == "subscription":
            return next_run_at(plan, time.time(), self.started_at)
        key = plan["key"].split(":", 1)[1]
        anchor = max(plan["last_run_at"], plan["schedule_updated_at"])
        if plan["cron"]:
            return after(plan, max(anchor, self.started_at))
        if key == "health":
            anchor = max(anchor, self.e._health_at, self.started_at)
        if key == "settle":
            anchor = max(anchor, self.e._settled_at)
        if key == "orphans":
            last = await self.db.last_tidy_job()
            if last:
                anchor = max(anchor, last["created_at"])
                return after(plan, anchor)
        if key in self._startup:
            return int(time.time())
        return after(plan, anchor)

    async def list(self):
        context, latest, plans = await asyncio.gather(self._context(), self.store.latest(), self._plans())
        rows = []
        for plan in plans:
            system = plan["kind"] == "system"
            spec = SYSTEM_TASKS[plan["key"].split(":", 1)[1]] if system else None
            reason = await self._reason(plan, context)
            manual_reason = await self._reason(plan, context, manual=True)
            last = latest.get(plan["key"])
            rows.append({
                "key": plan["key"], "kind": plan["kind"], "name": spec.name if system else plan["name"],
                "target": "系统" if system else f"{plan['site']} · {plan.get('library_name') or plan['library_id']}",
                "subscription_id": None if system else plan["id"], "library_id": None if system else plan["library_id"],
                "cron": plan["cron"], "interval": plan["interval"], "timezone": plan["timezone"], "enabled": bool(plan["enabled"]),
                "state": reason[0] if reason else "failed" if last and last["status"] in ("failed", "interrupted") else "scheduled",
                "reason": reason[1] if reason else "等待下次计划时间", "next_run_at": await self.next_at(plan),
                "last_run": last, "last_job_id": plan.get("last_job_id"),
                "can_run": manual_reason is None, "run_blocked_reason": manual_reason[1] if manual_reason else "",
                "follows_pause": spec.follows_pause if spec else True, "destructive": spec.destructive if spec else False,
                "note": spec.note if spec else "完成首轮全量后自动增量；停机期间的 cron 不补跑",
                "startup": bool(spec and spec.startup and not plan["cron"]),
            })
        return rows

    async def update(self, key, fields):
        async with self._lock:
            plan = await self.plan(key)
            if plan["kind"] == "subscription":
                await self.db.update_subscription(plan["id"], **fields)
            else:
                identifier = key.split(":", 1)[1]
                await self.store.update(identifier, fields)
                # 编辑计划不触发启动策略，也不立即执行任务。
                self._startup.discard(identifier)
        return {"ok": True}

    async def track_job(self, key, job_id):
        """原业务页面发起的单库操作也进入历史，不重置所有库的自动周期。"""
        async with self._lock:
            old = await self.db._one("SELECT id FROM schedule_runs WHERE job_id=?", (job_id,))
            if old:
                return
            run_id = await self.store.begin(key, "manual", None)
            await self.store.attach(run_id, job_id)
            await self.store.reconcile(job_id)

    async def run(self, key, *, trigger="manual", planned_at=None, request_key=None, mode="auto"):
        async with self._lock:
            plan = await self.plan(key)
            if request_key:
                if len(request_key) > 128:
                    raise ValueError("提交标识过长")
                old = await self.store.request(request_key)
                if old:
                    if old["schedule_key"] != key:
                        raise ValueError("同一提交标识不能用于不同任务")
                    return {"run_id": old["id"], "job_id": old["job_id"]}
            reason = await self._reason(plan, await self._context(), manual=trigger == "manual", event=trigger == "event")
            if reason:
                if trigger == "manual":
                    raise ValueError(reason[1])
                return None
            if trigger == "schedule":
                # 取到期时间后计划可能被编辑，持锁重算，避免按旧计划提前执行。
                planned_at = await self.next_at(plan)
                if planned_at is None or planned_at > time.time():
                    return None
            run_id = await self.store.begin(key, trigger, planned_at, request_key)
            try:
                if plan["kind"] == "subscription":
                    job_id = await self.e.run_subscription(plan["id"], mode if trigger == "manual" else "incremental")
                    await self.store.attach(run_id, job_id)
                else:
                    identifier = key.split(":", 1)[1]
                    if identifier in ("external_locate", "orphans"):
                        priority = 20 if trigger == "manual" else 0
                        job_id = await (self.e.strm.create_locate(automatic=True, priority=priority) if identifier == "external_locate"
                                        else self.e.strm.create_tidy(automatic=True, priority=priority))
                        await self.store.attach(run_id, job_id)
                    else:
                        job_id = None
                        task = asyncio.create_task(self._execute(identifier, run_id), name=f"schedule-{identifier}")
                        self._tasks[identifier] = task
                    await self.db._write("UPDATE system_schedules SET last_run_at=? WHERE key=?", (int(time.time()), identifier), invalidate=False)
                    self._startup.discard(identifier)
                return {"run_id": run_id, "job_id": job_id}
            except Exception as exc:
                await self.store.finish(run_id, "failed", error=str(exc))
                raise

    async def _execute(self, key, run_id):
        try:
            await self.store.started(run_id)
            if key == "settle":
                result = await self.e.settle()
            elif key == "health":
                self.e.start_health_check()
                await self.e._health_task
                result = {"hosts": len(self.e.resolver.health.hosts)}
            elif key == "trash":
                removed = 0
                async with self.e.file_changes.normal():
                    for lib in list(self.e.libs.values()):
                        if lib["external_dir"] and not lib.get("operation_pending"):
                            work = asyncio.create_task(asyncio.to_thread(purge_trash, self.e.writer.external_root(lib), self.e.store.current.trash_days))
                            try:
                                removed += await asyncio.shield(work)
                            except asyncio.CancelledError:
                                # 文件操作开始后等本次目录处理收尾，再释放文件变更保护。
                                await work
                                raise
                result = {"removed": removed, **{k: v for k, v in (await self.e.maintenance.purge_expired()).items() if v}}
            elif key == "play_history":
                from .play_history import PlaybackHistory
                result = await PlaybackHistory(self.db, self.e.store).cleanup()
            await self.store.finish(run_id, "done", result)
        except asyncio.CancelledError:
            await self.store.finish(run_id, "interrupted", error="服务退出，执行未完成")
            raise
        except Exception as exc:
            await self.store.finish(run_id, "failed", error=str(exc))
            log.exception("系统定时任务 %s 执行失败", key)
        finally:
            self._tasks.pop(key, None)

    async def due_subscriptions(self):
        for sub in await self.db.list_subscriptions():
            if not (sub["enabled"] and sub["initialized"] and has_schedule(sub)):
                continue
            planned = scheduled_after(sub, schedule_anchor(sub, self.started_at))
            if planned <= time.time():
                await self.run(f"subscription:{sub['id']}", trigger="schedule", planned_at=planned)

    async def due_system(self, key):
        plan = await self.plan("system:" + key)
        planned = await self.next_at(plan)
        if planned is not None and planned <= time.time():
            return await self.run(plan["key"], trigger="schedule", planned_at=planned)

    async def tick(self):
        await self.store.reconcile()
        try:
            await self.due_subscriptions()
        except Exception:
            log.exception("订阅调度失败")
        for key in SYSTEM_TASKS:
            try:
                if key == "settle" and self.e._settle_wanted:
                    await self.run("system:settle", trigger="event")
                else:
                    await self.due_system(key)
            except Exception:
                log.exception("定时任务 %s 调度失败", key)
