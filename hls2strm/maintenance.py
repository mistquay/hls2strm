"""库维护协调服务，复用现有后台队列和数据库事务。"""

import asyncio
from collections import Counter
import json
import os
import shutil
from pathlib import Path
import time

from . import maintenance_execute as execute, maintenance_plan as plans, maintenance_scan as scanner
from .maintenance_files import entries, validate, walk
from .maintenance_query import AssetFilter, list_assets
from .maintenance_progress import ScanProgress
from .storage import safe_directory, next_batch


class Maintenance:
    def __init__(self, engine):
        self.e, self.db = engine, engine.db
        self.scan_lock = asyncio.Lock()
        self.scan_create_lock = asyncio.Lock()

    def roots(self, lib):
        # 输出目录是整理前的收件目录，MDCNG 的最终目录才是维护对象。
        return [(self.e.writer.external_root(lib) or self.e.writer.library_root(lib)).absolute()]

    def configuration(self):
        return [{"id": lib["id"], "roots": [str(r) for r in self.roots(lib)],
                 "inbox": str(self.e.writer.library_root(lib).absolute()), "archive": str(self.e.store.archive_dir.absolute()),
                 "missing_policy": lib.get("missing_policy", "restore"), "versions": lib["versions"]}
                for lib in sorted(self.e.libs.values(), key=lambda l: l["id"])]

    @staticmethod
    def has_metadata_filter(query):
        return any(getattr(query, key) for key in ("categories", "tags", "models", "maker", "director", "series",
                    "release_from", "release_to", "year_from", "year_to", "duration_min", "duration_max"))

    async def create(self, kind, params):
        names = {"maintenance_scan": "扫描磁盘与 NFO", "maintenance_preview": "媒体清理预览",
                 "maintenance_restore_preview": "回收恢复预览"}
        if kind == "maintenance_scan":
            params["library_ids"] = sorted(set(params.get("library_ids") or self.e.libs))
            for lid in params["library_ids"]:
                self.e._library(lid)
            async with self.scan_create_lock:
                active = await self.db._one("SELECT id,params FROM jobs WHERE kind='maintenance_scan' AND status IN ('running','paused') ORDER BY id LIMIT 1")
                if active:
                    previous = json.loads(active["params"])
                    if (previous.get("library_ids") == params["library_ids"]
                            and (previous.get("force", False) or not params.get("force", False))):
                        return active["id"]
                    raise ValueError(f"扫描任务 #{active['id']} 尚未结束，请先完成或取消，再扫描其他范围")
                job = await self.db.create_job(kind, names[kind], params)
                await self.db.update_job(job, state={"scan_phase": "queued", "updated_at": int(time.time())})
                await self.db.add_tasks(job, kind, ["all"], 20)
                self.e.notify()
                return job
        name = "无可用源归档预览" if kind == "maintenance_preview" and params.get("request", {}).get("action") == "unavailable" else names[kind]
        job = await self.db.create_job(kind, name, params)
        await self.db.add_tasks(job, kind, ["all"], 20)
        self.e.notify()
        return job

    async def scan(self, job, task):
        progress = ScanProgress(self.e, job["id"])
        await progress.phase("waiting_lock" if self.scan_lock.locked() else "preparing")
        # 兼容升级前已排队的重复扫描；等待期间也响应暂停/取消。
        while True:
            await self.e.checkpoint(job["id"])
            try:
                await asyncio.wait_for(self.scan_lock.acquire(), timeout=1)
                break
            except asyncio.TimeoutError:
                await progress.publish()
        try:
            job = {**job, "_generation": -(time.time_ns() // 1000)}
            await self.refresh_roots()
            lids = job["params"]["library_ids"]
            for index, lid in enumerate(lids, 1):
                await progress.phase("preparing", library_id=lid, library_name=self.e._library(lid)["name"],
                                     library_index=index, library_total=len(lids), root="")
                await scanner.scan(self, job, {"target": str(lid)}, progress=progress)
            from .maintenance_files import area_size
            await progress.phase("archive", root=str(self.e.store.archive_dir))
            try:
                progress.counts["archive_bytes"] = await progress.wait(asyncio.to_thread(area_size, self.e.store.archive_dir, progress.visiting))
            except (OSError, ValueError):
                progress.counts["archive_unavailable"] = 1
            await progress.phase("pruning", root="")
            await self.prune_snapshots()
            await progress.phase("done", partial_failure=bool(progress.counts["failed_roots"]))
        finally:
            self.scan_lock.release()

    async def prune_snapshots(self):
        # 文件预览已完整复制资产证据，不依赖旧资产行；只保留每个根的成功基线。
        while rows := await self.db._all("""SELECT a.id FROM library_assets a WHERE NOT EXISTS (
            SELECT 1 FROM maintenance_roots r WHERE r.library_id=a.library_id AND r.root=a.root AND r.scan_id=a.scan_id)
            ORDER BY a.id LIMIT 500"""):
            ids = json.dumps([r["id"] for r in rows])
            async with self.db._tx(invalidate=False) as conn:
                await conn.execute("DELETE FROM asset_facets WHERE asset_id IN (SELECT value FROM json_each(?))", (ids,))
                await conn.execute("DELETE FROM library_assets WHERE id IN (SELECT value FROM json_each(?))", (ids,))

    async def preview(self, job, task):
        await plans.preview(self, job, task)

    async def apply(self, job, task):
        await execute.apply(self, job, task)

    async def restore_preview(self, job, task):
        await execute.restore_preview(self, job, task)

    async def restore(self, job, task):
        await execute.restore(self, job, task)

    async def check_roots(self, library_ids, *, allow_empty=False):
        for lid in library_ids:
            lib = self.e._library(lid)
            if lib.get("operation_pending"):
                raise ValueError("库配置有未完成变更")
            for root in self.roots(lib):
                row = await self.db._one("SELECT * FROM maintenance_roots WHERE library_id=? AND root=?", (lid, str(root)))
                if not row or row["health"] not in ("ok", "empty"):
                    raise ValueError(f"目录尚无完整成功扫描或不可达：{root}")
                if row["health"] == "empty" and not (allow_empty and row["baseline"]):
                    prefix = str(root).rstrip("/\\") + os.sep
                    previously_written = bool(await self.db._one("SELECT 1 FROM outputs WHERE library_id=? AND lower(substr(strm_path,1,?))=lower(?) LIMIT 1", (lid, len(prefix), prefix)))
                    if previously_written:
                        raise ValueError(f"目录为空，需要已有基线及单独确认整库清空：{root}")
                if await asyncio.to_thread(safe_directory, root) != json.loads(row["identity"]):
                    raise ValueError(f"根目录身份改变，请检查挂载：{root}")

    async def check_asset_root(self, asset):
        root = Path(asset["root"])
        lib = self.e._library(asset["library_id"])
        if lib.get("operation_pending"):
            raise ValueError("资产所在库有未完成配置变更")
        inbox = asset["data"].get("inbox") and root == self.e.writer.library_root(lib).absolute()
        if root not in self.roots(lib) and not inbox:
            raise ValueError("资产根目录不再属于配置库")
        if await asyncio.to_thread(safe_directory, root) != asset["data"]["root_identity"]:
            raise ValueError("资产根目录已被替换")

    async def find_paths(self, library_ids):
        keys = {slug: vid for vid, slug in await self.db.video_keys()}
        cdn = await self.db.cdn_video_map()
        result = {}
        for lid in library_ids:
            paths = {}
            try:
                lib = self.e._library(lid)
                # 整片退库前仍核对收件区是否有副本；它不进入维护索引或清理清单。
                roots = dict.fromkeys([*self.roots(lib), self.e.writer.library_root(lib).absolute()])
                for root in roots:
                    await asyncio.to_thread(safe_directory, root)
                    iterator = walk(root, keys, cdn)
                    try:
                        while rows := await next_batch(iterator):
                            for data in rows:
                                for entry in data["strms"]:
                                    paths.setdefault(entry["video_id"], set()).add(str(root / data["path"] / entry["path"]))
                    finally:
                        iterator.close()
                for row in await self.db._all("""SELECT a.* FROM library_assets a JOIN maintenance_roots r
                    ON r.library_id=a.library_id AND r.root=a.root AND r.scan_id=a.scan_id
                    WHERE a.library_id=? AND r.health!='retired' AND json_extract(a.data,'$.manual_binding')=1 AND a.status='present'""", (lid,)):
                    a = scanner.decode_asset(row)
                    for f in a["data"]["strms"]:
                        path = Path(a["root"]) / a["path"] / f["path"]
                        if path.is_file():
                            paths.setdefault(a["video_id"], set()).add(str(path))
                result[lid] = paths
            except (OSError, ValueError) as error:
                result[lid] = str(error)
        return result

    async def validate_outputs(self, item, data):
        libs = set(data.get("library_ids", [a["library_id"] for a in data["assets"]]))
        fields = lambda rows: sorted((r["library_id"], r["strm_path"], r["via"]) for r in rows if r["library_id"] in libs)
        if fields(await self.db.get_outputs(item["video_id"])) != fields(data["outputs"]):
            raise ValueError("预览后输出关联或路径已改变，请重新核对")

    async def validate_assets(self, assets, request, *, paths=None):
        libs = {a["library_id"] for a in assets}
        if request.action != "resources" and paths is None:
            paths = await self.find_paths(libs)
        for a in assets:
            await self.check_asset_root(a)
            if request.query.source == "effective" and self.has_metadata_filter(request.query):
                current = scanner.source_metadata(await self.db.get_video_by_id(a["video_id"]))
                if current != a["data"].get("source_metadata", {}):
                    raise ValueError("源站补缺资料已变化，请重新扫描预览")
            if a["status"] == "present":
                await asyncio.to_thread(validate, Path(a["root"]), a["data"])
            elif request.action != "reconcile":
                raise ValueError("文件状态不是在盘")
            elif a["data"].get("recorded_path") and Path(a["data"]["recorded_path"]).exists():
                raise ValueError("原路径文件已恢复，不能按缺失退库")
        if request.action != "resources":
            for lid in libs:
                if isinstance(paths.get(lid), str):
                    raise ValueError(paths[lid])
                for vid in {a["video_id"] for a in assets}:
                    expected = {str(Path(a["root"]) / a["path"] / f["path"]) for a in assets if a["library_id"] == lid and a["video_id"] == vid
                                for f in a["data"]["files"] if f["kind"] == "strm"}
                    actual = paths.get(lid, {}).get(vid, set())
                    if request.action == "reconcile" and (actual or any(Path(p).exists() for p in expected)):
                        raise ValueError("影片文件已经恢复或移动到其他位置")
                    if request.action == "remove" and actual != expected:
                        raise ValueError("影片副本范围已改变，请重新扫描预览")

    async def validate_resuming(self, data, files, request):
        completed = {f["source"] for f in files if f["state"] in ("trashed", "deleted", "absent", "prepared")}
        for a in data["assets"]:
            await self.check_asset_root(a)
            if request.action == "reconcile":
                await self.validate_assets(data["assets"], request)
                continue
            directory = Path(a["root"]) / a["path"]
            await asyncio.to_thread(validate, Path(a["root"]), a["data"], strict=False)
            actual, _ = await asyncio.to_thread(entries, directory)
            before = {f["path"]: (f.get("fingerprint"), f.get("sha256")) for f in a["data"]["files"]}
            if a["data"].get("partial"):
                actual = [f for f in actual if f["path"] in before]
            for f in actual:
                if f["path"] not in before or before[f["path"]] != (f.get("fingerprint"), f.get("sha256")):
                    raise ValueError("中断后出现新文件或文件改变，保留现场")
            after = {f["path"] for f in actual}
            if any(str(directory / name) not in completed for name in set(before) - after):
                raise ValueError("非本任务处理的文件消失，请重新核对")

    async def approve(self, preview_id, token):
        import secrets
        async with self.db._tx() as conn:
            preview = await self.db.get_job(preview_id)
            if (not preview or preview["kind"] not in ("maintenance_preview", "maintenance_restore_preview")
                or preview["status"] != "done" or not preview["state"].get("ready") or preview["state"].get("partial_failure")
                or not token or not secrets.compare_digest(token, preview["state"].get("preview_token", ""))):
                raise ValueError("预览无效或尚未完成")
            if preview["state"].get("apply_job_id"):
                return preview["state"]["apply_job_id"]
            if not preview["state"].get("eligible"):
                raise ValueError("没有可执行条目")
            if preview["state"]["configuration"] != self.configuration():
                raise ValueError("库配置已变化，请重新预览")
            kind = "maintenance_apply" if preview["kind"] == "maintenance_preview" else "maintenance_restore"
            params = {"preview_id": preview_id, "retention_days": preview["params"].get("request", {}).get("retention_days", 7)}
            name = "执行已确认媒体清理" if kind == "maintenance_apply" else "恢复已确认回收文件"
            if kind == "maintenance_apply" and preview["params"].get("request", {}).get("action") == "unavailable":
                name = "执行无可用源归档"
            cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES(?,?,?,'running',?)", (kind,
                name, json.dumps(params), int(time.time())))
            job_id = cur.lastrowid
            await conn.execute("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,?,'all',20,?)", (job_id, kind, int(time.time())))
            await conn.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps({**preview["state"], "apply_job_id": job_id}), preview_id))
        self.e.notify()
        return job_id

    async def item_counts(self, item_id):
        item = await self.db._one("SELECT state,data FROM maintenance_items WHERE id=?", (item_id,))
        state = {item["state"]: 1}
        source_item = json.loads(item["data"]).get("original_item", item_id)
        for row in await self.db._all("SELECT state,COUNT(*) AS n,SUM(json_extract(fingerprint,'$[2]')) AS bytes FROM cleanup_files WHERE item_id=? GROUP BY state", (source_item,)):
            state[row["state"] + "_files"] = row["n"]
            state[row["state"] + "_bytes"] = row["bytes"] or 0
        return state

    async def progress(self, job_id, preview_id, item_id=None, before=None):
        if item_id is not None:
            after = await self.item_counts(item_id)
            current = await self.db.get_job(job_id)
            state = current["state"]
            for key in set(before) | set(after):
                state[key] = max(0, state.get(key, 0) - before.get(key, 0) + after.get(key, 0))
            state["partial_failure"] = bool(state.get("failed"))
            await self.db.update_job(job_id, state=state)
            return
        groups = await self.db._all("SELECT state,COUNT(*) AS n FROM maintenance_items WHERE preview_id=? GROUP BY state", (preview_id,))
        state = {r["state"]: r["n"] for r in groups}
        for row in await self.db._all("SELECT f.state,COUNT(*) AS n,SUM(json_extract(f.fingerprint,'$[2]')) AS bytes FROM maintenance_items i JOIN cleanup_files f ON f.item_id=COALESCE(json_extract(i.data,'$.original_item'),i.id) WHERE i.preview_id=? GROUP BY f.state", (preview_id,)):
            state[row["state"] + "_files"] = row["n"]
            state[row["state"] + "_bytes"] = row["bytes"] or 0
        state["partial_failure"] = bool(state.get("failed"))
        current = await self.db.get_job(job_id)
        if current["state"].get("space"):
            state["space"] = current["state"]["space"]
        await self.db.update_job(job_id, state=state)

    async def record_space(self, job_id, preview_id, phase):
        job = await self.db.get_job(job_id)
        space = job["state"].get("space", {})
        roots = await self.db._all("SELECT DISTINCT json_extract(a.value,'$.root') AS root FROM maintenance_items i,json_each(i.data,'$.assets') a WHERE i.preview_id=?", (preview_id,))
        for row in roots:
            previous = space.setdefault(row["root"], {})
            if phase == "before" and phase in previous:
                continue
            try:
                usage = await asyncio.to_thread(shutil.disk_usage, row["root"])
                previous.update({phase: usage.free, "total": usage.total})
            except OSError:
                previous[phase] = None
        await self.db.update_job(job_id, state={**job["state"], "space": space})

    async def items(self, job_id, offset=0, limit=50):
        job = await self.db.get_job(job_id)
        if not job or not job["kind"].startswith("maintenance_"):
            raise ValueError("维护任务不存在")
        pid = job["params"].get("preview_id", job_id)
        rows = await self.db._all("SELECT * FROM maintenance_items WHERE preview_id=? ORDER BY id LIMIT ? OFFSET ?", (pid, limit, offset))
        items = []
        for row in rows:
            item = dict(row)
            data = json.loads(item["data"])
            item["data"] = {k: v for k, v in data.items() if k != "assets"}
            item["data"]["assets"] = [{"id": a["id"], "library_id": a["library_id"], "root": a["root"], "path": a["path"],
                                       "nfo_status": a["nfo_status"], "nfo_path": a["data"].get("nfo_path", "")} for a in data["assets"]]
            items.append(item)
        count = await self.db._one("SELECT COUNT(*) AS n FROM maintenance_items WHERE preview_id=?", (pid,))
        return {"job": job, "items": items, "total": count["n"]}

    async def local_details(self, video_id):
        result = await list_assets(self.db, AssetFilter(video_ids=[video_id]), limit=100)
        exclusions = [dict(r) for r in await self.db._all("SELECT * FROM collection_exclusions WHERE video_id=?", (video_id,))]
        return {**result, "exclusions": exclusions}

    async def refresh_roots(self):
        current = {(lib["id"], str(root)) for lib in self.e.libs.values() for root in self.roots(lib)}
        for row in await self.db._all("SELECT library_id,root FROM maintenance_roots"):
            if (row["library_id"], row["root"]) not in current:
                await self.db._write("UPDATE maintenance_roots SET health='retired' WHERE library_id=? AND root=?", (row["library_id"], row["root"]))

    async def bind_asset(self, asset_id, video_id):
        from .codes import code_key, edition
        async with self.e.file_changes.exclusive():
            row = await self.db._one("SELECT a.* FROM library_assets a JOIN maintenance_roots r ON r.library_id=a.library_id AND r.root=a.root AND r.scan_id=a.scan_id WHERE a.id=?", (asset_id,))
            video = await self.db.get_video_by_id(video_id)
            if not row or not video:
                raise ValueError("资产或影片不存在")
            asset = scanner.decode_asset(row)
            if asset["video_id"] or asset["status"] != "unmanaged" or asset["nfo_status"] != "valid":
                raise ValueError("只允许关联具有有效 NFO 的未纳管目录")
            codes = asset["metadata"].get("codes", [])
            if not codes or any(code_key(code, video.get("maker", "")) != video["code_key"] for code in codes):
                raise ValueError("NFO 番号与所选影片不一致")
            if any(edition(code, False, video.get("maker", "")) for code in codes) and not video["uncensored"]:
                raise ValueError("NFO 的 -U 版本与所选影片不一致")
            await self.check_asset_root(asset)
            await asyncio.to_thread(validate, Path(asset["root"]), asset["data"])
            data = asset["data"]
            if any(Path(f["path"]).parent != Path('.') for f in data["files"] if f["kind"] == "strm"):
                raise ValueError("包含嵌套影片文件，不允许按独占目录关联")
            data.update(video_id=video_id, status="present", manual_binding=True)
            data["strms"] = [{"path": f["path"], "video_id": video_id, "version": False} for f in data["files"] if f["kind"] == "strm"]
            await self.db._write("UPDATE library_assets SET video_id=?,status='present',data=? WHERE id=?", (video_id, json.dumps(data, ensure_ascii=False), asset_id))

    async def summaries(self, video_ids):
        if not video_ids:
            return {}
        rows = await self.db._all("""SELECT a.video_id,a.metadata FROM library_assets a JOIN maintenance_roots r
            ON r.library_id=a.library_id AND r.root=a.root AND r.scan_id=a.scan_id WHERE r.health='ok'
            AND a.status='present' AND a.nfo_status='valid' AND a.video_id IN (SELECT value FROM json_each(?))""", (json.dumps(video_ids),))
        groups = {}
        for row in rows:
            meta = json.loads(row["metadata"])
            value = {k: meta.get(k) for k in ("title", "release_date", "year", "categories", "models", "maker")}
            groups.setdefault(row["video_id"], {})[json.dumps(value, sort_keys=True)] = value
        return {vid: next(iter(values.values())) if len(values) == 1 else {"conflict": True} for vid, values in groups.items()}

    async def retry(self, job_id):
        job = await self.db.get_job(job_id)
        if not job or not job["kind"].startswith("maintenance_"):
            raise ValueError("维护任务不存在")
        if job["status"] in ("running", "paused") or (await self.db.task_counts(job_id)).get("running"):
            raise ValueError("任务尚未停止")
        if job["kind"] in ("maintenance_scan", "maintenance_preview", "maintenance_restore_preview"):
            return await self.create(job["kind"], job["params"])
        async with self.db._tx() as conn:
            await conn.execute("UPDATE tasks SET status='pending',attempts=0,next_run_at=0,last_error='' WHERE job_id=?", (job_id,))
            await conn.execute("UPDATE jobs SET status='running',finished_at=NULL WHERE id=?", (job_id,))
        self.e.notify()
        return job_id

    async def purge_expired(self):
        """仅清理本模块有文件日志、已完成且到期的回收文件。"""
        cursor, removed, failed = 0, 0, 0
        while rows := await self.db._all("""SELECT f.*,i.data FROM cleanup_files f JOIN maintenance_items i ON i.id=f.item_id
            JOIN jobs j ON j.kind='maintenance_apply' AND json_extract(j.params,'$.preview_id')=i.preview_id
            WHERE f.id>? AND f.state='trashed' AND j.status IN ('done','cancelled') AND i.state IN ('done','abandoned')
            AND COALESCE(json_extract(i.data,'$.request.disposition'),'trash')!='archive'
            AND json_extract(j.params,'$.expires_at')<?
            AND NOT EXISTS (SELECT 1 FROM jobs restoration JOIN maintenance_items ri
                ON ri.preview_id=json_extract(restoration.params,'$.preview_id')
                WHERE restoration.kind='maintenance_restore' AND restoration.status IN ('running','paused')
                AND json_extract(ri.data,'$.original_item')=i.id)
            ORDER BY f.id LIMIT 100""", (cursor, int(time.time()))):
            for row in rows:
                cursor = row["id"]
                data = json.loads(row["data"])
                root = execute.root_for(data, row["source"])
                try:
                    async with self.e.file_changes.exclusive():
                        for a in data["assets"]:
                            await self.check_asset_root(a)
                        path = Path(row["destination"])
                        await asyncio.to_thread(execute.checked_parent, path, root)
                        if path.exists():
                            if not await asyncio.to_thread(execute.same_file, path, json.loads(row["fingerprint"]), True):
                                raise ValueError("回收文件身份已变化，保留现场")
                            await asyncio.to_thread(path.unlink)
                            removed += 1
                        await self.db._write("UPDATE cleanup_files SET state='expired' WHERE id=? AND state='trashed'", (row["id"],))
                except (OSError, ValueError) as error:
                    failed += 1
                    await self.db._write("UPDATE cleanup_files SET error=? WHERE id=?", (str(error), row["id"]))
        return {"expired_files": removed, "failed_files": failed}

    async def abandon(self, job_id):
        job = await self.db.get_job(job_id)
        if not job or job["kind"] != "maintenance_apply" or job["status"] in ("running", "paused") or (await self.db.task_counts(job_id)).get("running"):
            raise ValueError("请先等待清理任务停止")
        async with self.e.file_changes.exclusive():
            rows = await self.db._all("SELECT * FROM maintenance_items WHERE preview_id=? AND state IN ('failed','running','planned')", (job["params"]["preview_id"],))
            for item in rows:
                data = json.loads(item["data"])
                request = plans.CleanupRequest.model_validate(data["request"])
                for a in data["assets"]:
                    await self.check_asset_root(a)
                files = await self.db._all("SELECT * FROM cleanup_files WHERE item_id=?", (item["id"],))
                # 未提交完成的意图先根据两端实际情况记账，不把已动过的文件当作没执行。
                for file in files:
                    if file["state"] != "prepared":
                        continue
                    if not Path(file["source"]).exists():
                        status = "trashed" if file["destination"] and Path(file["destination"]).exists() else "absent"
                        await self.db._write("UPDATE cleanup_files SET state=? WHERE id=?", (status, file["id"]))
                touched = [r for r in await self.db._all("SELECT * FROM cleanup_files WHERE item_id=? AND state IN ('trashed','deleted','absent')", (item["id"],))]
                async with self.db._tx() as conn:
                    if request.action == "resources":
                        for a in data["assets"]:
                            for kind in {f["kind"] for f in touched}:
                                await conn.execute("INSERT OR IGNORE INTO resource_exclusions VALUES(?,?,?,?)", (item["video_id"], a["library_id"], kind, job_id))
                        await conn.execute("DELETE FROM collection_exclusions WHERE video_id=? AND job_id=?", (item["video_id"], job_id))
                    elif touched:
                        for out in data["outputs"]:
                            if not Path(out["strm_path"]).exists():
                                await conn.execute("DELETE FROM outputs WHERE video_id=? AND library_id=?", (item["video_id"], out["library_id"]))
                        await conn.execute("UPDATE collection_exclusions SET reason='已放弃未完成清理，保留已执行范围的禁收' WHERE video_id=? AND job_id=?", (item["video_id"], job_id))
                    else:
                        await conn.execute("DELETE FROM collection_exclusions WHERE video_id=? AND job_id=?", (item["video_id"], job_id))
                    await conn.execute("UPDATE maintenance_items SET state='abandoned' WHERE id=?", (item["id"],))
        await self.progress(job_id, job["params"]["preview_id"])
        preview = await self.db.get_job(job["params"]["preview_id"])
        if preview["params"]["request"].get("disposition") != "archive":
            await self.db._write("UPDATE jobs SET params=json_set(params,'$.expires_at',?) WHERE id=?",
                                 (int(time.time()) + job["params"].get("retention_days", 7) * 86400, job_id))
