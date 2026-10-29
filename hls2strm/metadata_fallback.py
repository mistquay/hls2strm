"""Manual, independent metadata repair modes with frozen previews."""

import asyncio
from collections import Counter
import json
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .collection_policy import resources
from .errors import Blocked, FetchError, NotFound
from .maintenance_files import RESOURCE_TYPES, validate, walk
from .maintenance_scan import read_metadata
from .maintenance_unavailable import source_state
from .metadata_files import finish_files, prepare_files, release_prepared
from .metadata_patch import patch_nfo, picture
from .relocate import safe_path
from .storage import next_batch, safe_directory
from .writer import sanitize

MODES = {"missing": "补缺失 NFO / 图片", "fields": "已有 NFO 补空字段", "takeover": "接管未整理文件"}
log = logging.getLogger(__name__)


class RepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_id: int = Field(gt=0)
    mode: Literal["missing", "fields", "takeover"]
    images: bool = True
    failed_dir: str = ""
    allow_empty_external: bool = False


class MetadataFallback:
    def __init__(self, engine, data_dir):
        self.e, self.db = engine, engine.db
        self.directory = Path(data_dir) / "metadata-fallback"

    def configuration(self, request):
        lib = self.e._library(request.library_id)
        if not lib.get("external_dir") or lib.get("operation_pending"):
            raise ValueError("请选择没有待处理配置变更的外部整理库")
        external = self.e.writer.external_root(lib).absolute()
        inbox = self.e.writer.library_root(lib).absolute()
        roots = [external] if request.mode != "takeover" else [inbox]
        if request.failed_dir:
            if request.mode != "takeover":
                raise ValueError("失败目录仅用于未整理文件接管")
            failed = Path(request.failed_dir)
            if not failed.is_absolute() or ".." in failed.parts:
                raise ValueError("失败目录必须是服务端可见的绝对路径")
            configured = [self.e.store.archive_dir.absolute(), self.directory.parent.absolute()]
            for other in self.e.libs.values():
                configured.append(self.e.writer.library_root(other).absolute())
                if other.get("external_dir"):
                    configured.append(self.e.writer.external_root(other).absolute())
            if any(failed.is_relative_to(p) or p.is_relative_to(failed) for p in configured):
                raise ValueError("失败目录不能与库目录、归档目录或服务数据目录重叠")
            roots.append(failed)
        return {"library_id": lib["id"], "external": str(external), "inbox": str(inbox),
                "roots": [str(p) for p in roots]}

    async def create(self, request):
        self.configuration(request)
        job_id = await self.db.create_job("metadata_preview", "源站资料预览 · " + MODES[request.mode], request.model_dump())
        await self.db.add_tasks(job_id, "metadata_preview", ["all"], 20)
        self.e.notify()
        return job_id

    async def eligible(self, vid, lid):
        video = await self.db.get_video_by_id(vid)
        output = await self.db.get_output(vid, lid)
        if not video or video["status"] != "active" or not output:
            raise ValueError("没有活动影片或本库输出关联，不自动纳管")
        if await self.db.collection_blocked(vid, lid):
            raise ValueError("影片已禁止收录")
        usable, _ = await source_state(self.e.maintenance, vid)
        if not usable:
            raise ValueError("没有符合当前策略的可用源")
        return video, output, await resources(self.db, vid, lid)

    async def plan_item(self, request, root, asset, config, locations):
        data = {"mode": request.mode, "library_id": request.library_id, "root": str(root), "asset": asset,
                "source": str(root / asset["path"]), "changes": [], "files": [], "warnings": [], "code": ""}
        try:
            if asset["status"] != "present" or not asset["video_id"]:
                raise ValueError(asset.get("error") or "无法按 STRM 唯一识别影片")
            primaries = [s for s in asset["strms"] if not s["version"]]
            if len(primaries) != 1:
                raise ValueError("主 STRM 不唯一，先处理重复文件")
            video, output, banned = await self.eligible(asset["video_id"], request.library_id)
            data.update(code=video["code"], title=video["title"], output=output,
                        banned=sorted(banned), root_identity=await asyncio.to_thread(safe_directory, root))
            data["external_identity"] = await asyncio.to_thread(safe_directory, Path(config["external"]))
            data["strm"] = primaries[0]["path"]
            if Path(data["strm"]).parent != Path("."):
                raise ValueError("共享目录不自动处理")
            local, _, status = await asyncio.to_thread(read_metadata, root, asset, video, None, True)
            if status in ("invalid", "conflict"):
                raise ValueError(asset.get("nfo_error") or "NFO 无效或归属冲突")
            if any(f["kind"] == "protected" for f in asset["files"]):
                raise ValueError("目录含受保护文件或链接")
            source = Path(data["source"])
            target = source
            if request.mode == "takeover":
                existing = locations.get(video["id"], [])
                if any(Path(p).is_relative_to(Path(config["external"])) for p in existing):
                    raise ValueError("外部目录已有该影片，不重复接管")
                if any(Path(p) != source / s["path"] for p in existing for s in primaries):
                    raise ValueError("收件目录存在其他副本，需先确认")
                if asset["path"] == "." or any(f["kind"] not in RESOURCE_TYPES | {"strm"} for f in asset["files"]):
                    raise ValueError("接管仅支持独立影片目录，未知文件、实体媒体或共享根目录不移动")
                target = Path(config["external"]) / "_hls补全" / sanitize(video["slug"])
                if not safe_path(target, Path(config["external"])):
                    raise ValueError("接管目标包含链接或越界")
                if os.path.lexists(target):
                    raise ValueError("接管目标目录已存在，不合并或覆盖")
            if request.mode == "fields" and status == "missing":
                raise ValueError("没有 NFO，请使用“补缺失 NFO / 图片”")
            data["target"] = str(target)
            data["nfo_path"] = asset.get("nfo_path") or str(Path(data["strm"]).with_suffix(".nfo"))
            nfo_bytes, changes = None, []
            if "nfo" not in banned and (status == "missing" or request.mode in ("fields", "takeover")):
                original = (source / data["nfo_path"]).read_bytes() if status == "valid" else None
                nfo_bytes, changes = patch_nfo(video, original)
                if status == "missing" and not (video.get("title") and video["title"].casefold() not in
                                               (video["code"].casefold(), video["slug"].casefold())):
                    raise ValueError("本地没有有效标题，请先补充资料，不生成仅有番号的 NFO")
                if changes:
                    data["files"].append({"path": str(target / data["nfo_path"]), "action": "新增 NFO" if original is None else "补空字段"})
            data["nfo_xml"] = nfo_bytes.decode("utf-8") if changes else None
            data["changes"] = changes
            data["images"] = {}
            if request.images and request.mode != "fields":
                cover = video.get("cover_url") or video.get("thumb_url")
                if cover:
                    for kind in ("poster", "fanart"):
                        if kind not in banned and not any(f["kind"] == kind for f in asset["files"]):
                            rel = str(Path(data["strm"]).with_suffix("")) + f"-{kind}.jpg"
                            data["images"][rel] = cover
                            data["files"].append({"path": str(target / rel), "action": "下载图片"})
                    if not video.get("cover_url"):
                        data["warnings"].append("使用列表缩略图降级补图，不保证海报质量")
                else:
                    data["warnings"].append("数据库没有图片地址，无法补图")
            data["warnings"].append("不导出来源不明的历史发行日期，不生成剧情或评分；图片不自动裁剪")
            if banned:
                data["warnings"].append("遵守资源禁补：" + "、".join(sorted(banned)))
            if request.mode == "takeover":
                if status == "missing" and not data["nfo_xml"]:
                    raise ValueError("不能生成 NFO，暂不接管")
                data["files"] = [{"path": str(target / f["path"]), "action": "接管移动"} for f in asset["files"]] + data["files"]
            elif not data["files"]:
                raise ValueError("没有可补内容（已有资料保留，禁补资源不重建）")
            return data, "planned", ""
        except (OSError, ValueError) as error:
            return data, "skipped", str(error)

    async def preview(self, job, task):
        request = RepairRequest.model_validate(job["params"])
        config = self.configuration(request)
        await self.db.update_job(job["id"], state={"ready": False})
        await self.db._write("DELETE FROM maintenance_items WHERE preview_id=?", (job["id"],))
        counts = Counter()
        try:
            await asyncio.to_thread(safe_directory, Path(config["external"]))
            locations = await self.e.strm.index(self.e._library(request.library_id), max_age=0) if request.mode == "takeover" else {}
            if (request.mode == "takeover" and not request.allow_empty_external
                    and self.e.strm.external_problem(self.e._library(request.library_id))):
                raise ValueError("外部目录没有可识别 STRM；请核对挂载，确认后显式允许空目录接管")
            keys = {slug: vid for vid, slug in await self.db.video_keys()}
            cdn = await self.db.cdn_video_map()
            for name in config["roots"]:
                root = Path(name)
                await asyncio.to_thread(safe_directory, root)
                iterator = walk(root, keys, cdn)
                try:
                    while rows := await next_batch(iterator):
                        for asset in rows:
                            await self.e.checkpoint(job["id"])
                            if not asset["strms"] and not any(f["kind"] == "strm" for f in asset["files"]):
                                continue
                            data, state, error = await self.plan_item(request, root, asset, config, locations)
                            await self.db._write("INSERT INTO maintenance_items(preview_id,video_id,data,state,error) VALUES(?,?,?,?,?)",
                                                 (job["id"], asset["video_id"], json.dumps(data, ensure_ascii=False), state, error))
                            counts["checked"] += 1
                        await self.db.update_job(job["id"], state=dict(counts))
                finally:
                    iterator.close()
            # Two failure directories/copies must never race for the same destination.
            await self.db._write("""UPDATE maintenance_items SET state='skipped',error='存在同影片多个候选目录，请先确认副本'
                WHERE preview_id=? AND video_id IN (SELECT video_id FROM maintenance_items WHERE preview_id=?
                GROUP BY video_id HAVING COUNT(*)>1)""", (job["id"], job["id"]))
            for r in await self.db._all("SELECT state,COUNT(*) AS n FROM maintenance_items WHERE preview_id=? GROUP BY state", (job["id"],)):
                counts["eligible" if r["state"] == "planned" else "skipped"] = r["n"]
            if config != self.configuration(request):
                raise ValueError("扫描期间库配置改变，请重新预览")
            await self.db.update_job(job["id"], state={**counts, "ready": True, "configuration": config,
                                                      "preview_token": secrets.token_urlsafe(24)})
        except (OSError, ValueError) as error:
            await self.db.update_job(job["id"], state={**counts, "ready": False, "partial_failure": True, "error": str(error)})

    async def approve(self, preview_id, token, paused):
        if not paused:
            raise ValueError("请先暂停对应外部刮削写入并确认")
        async with self.db._tx() as conn:
            job = await self.db.get_job(preview_id)
            if (not job or job["kind"] != "metadata_preview" or job["status"] != "done"
                    or not job["state"].get("ready") or not token
                    or not secrets.compare_digest(token, job["state"].get("preview_token", ""))):
                raise ValueError("预览尚未完成或确认凭证无效")
            if job["state"].get("apply_job_id"):
                return job["state"]["apply_job_id"]
            if not job["state"].get("eligible"):
                raise ValueError("没有可执行条目")
            if self.configuration(RepairRequest.model_validate(job["params"])) != job["state"]["configuration"]:
                raise ValueError("库配置改变，请重新预览")
            cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES('metadata_apply',?,?,'running',?)",
                                     ("源站资料补全 · " + MODES[job["params"]["mode"]], json.dumps({"preview_id": preview_id}), int(time.time())))
            jid = cur.lastrowid
            await conn.execute("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,'metadata_apply','all',20,?)", (jid, int(time.time())))
            await conn.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps({**job["state"], "apply_job_id": jid}), preview_id))
        self.e.notify()
        return jid

    async def save_item(self, item_id, data, state, error=""):
        await self.db._write("UPDATE maintenance_items SET data=?,state=?,error=? WHERE id=?",
                             (json.dumps(data, ensure_ascii=False), state, error, item_id))

    async def apply(self, job, task):
        preview = await self.db.get_job(job["params"]["preview_id"])
        request = RepairRequest.model_validate(preview["params"])
        config = preview["state"]["configuration"]
        locations = await self.e.strm.index(self.e._library(request.library_id), max_age=0) if request.mode == "takeover" else {}
        cursor = 0
        while rows := await self.db._all("SELECT * FROM maintenance_items WHERE preview_id=? AND id>? ORDER BY id LIMIT 50", (preview["id"], cursor)):
            cursor = rows[-1]["id"]
            for row in rows:
                await self.e.checkpoint(job["id"])
                if row["state"] in ("done", "skipped"):
                    continue
                data = json.loads(row["data"])
                try:
                    if not data.get("prepared"):
                        images = {}
                        fetched = {}
                        for rel, url in data["images"].items():
                            await self.e.checkpoint(job["id"])
                            if url not in fetched:
                                fetched[url] = await self.e.fetcher.get_bytes(url)
                            images[rel] = await asyncio.to_thread(picture, fetched[url])
                    await self.e.checkpoint(job["id"])
                    async with self.e.file_changes.exclusive():
                        await self.e.checkpoint(job["id"])
                        if self.configuration(request) != config:
                            raise ValueError("库配置变化，保留现场")
                        if not safe_path(Path(data["target"]) / data["strm"], Path(config["external"])):
                            raise ValueError("目标路径包含链接或越界，保留现场")
                        for root, key in ((data["root"], "root_identity"), (config["external"], "external_identity")):
                            if await asyncio.to_thread(safe_directory, Path(root)) != data[key]:
                                raise ValueError("目录身份变化，保留现场")
                        _, output, banned = await self.eligible(row["video_id"], request.library_id)
                        if sorted(banned) != data["banned"]:
                            raise ValueError("资源禁补规则变化，请重新预览")
                        expected = {data["output"]["strm_path"]}
                        if data.get("prepared"):
                            expected.add(str(Path(data["target"]) / data["strm"]))
                        if output["strm_path"] not in expected or output["via"] != data["output"]["via"]:
                            raise ValueError("输出关联改变，请重新预览")
                        if request.mode == "takeover":
                            owned = set(data.get("prepared", {}).get("writes", {}))
                            if any(Path(p).is_relative_to(Path(config["external"])) and p not in owned
                                   for p in locations.get(row["video_id"], [])):
                                raise ValueError("外部目录已出现其他副本，跳过接管")
                        if not data.get("prepared"):
                            await asyncio.to_thread(validate, Path(data["root"]), data["asset"])
                            if request.mode == "takeover":
                                if (not request.allow_empty_external
                                        and self.e.strm.external_problem(self.e._library(request.library_id))):
                                    raise ValueError("外部目录变为空或不可用，停止接管")
                                if os.path.lexists(data["target"]):
                                    raise ValueError("接管目标已存在，不覆盖")
                            data["prepared"] = await asyncio.to_thread(prepare_files, data, self.directory / str(row["id"]),
                                data["nfo_xml"].encode("utf-8") if data["nfo_xml"] else None, images)
                            await self.save_item(row["id"], data, "running")
                        await asyncio.to_thread(finish_files, data)
                        if request.mode == "takeover":
                            await self.db.set_output_paths(request.library_id, [(str(Path(data["target"]) / data["strm"]), row["video_id"])])
                            self.e.strm.forget(self.e._library(request.library_id), row["video_id"])
                            self.e.strm.remember(self.e._library(request.library_id), row["video_id"], str(Path(data["target"]) / data["strm"]))
                        await self.save_item(row["id"], data, "done")
                        # Old snapshots must not present pre-patch metadata as current.
                        await self.db._write("UPDATE maintenance_roots SET health='stale',scan_id=NULL WHERE library_id=?", (request.library_id,))
                        try:
                            await asyncio.to_thread(release_prepared, data, self.directory / str(row["id"]))
                        except (OSError, ValueError):
                            log.warning("补全已完成，准备文件待人工清理：%s", self.directory / str(row["id"]), exc_info=True)
                except (OSError, ValueError, RuntimeError, FetchError, NotFound, Blocked) as error:
                    await self.save_item(row["id"], data, "failed", str(error))
                await self.progress(job["id"], preview["id"])
        self.e.strm._index.pop(request.library_id, None)

    async def progress(self, jid, pid):
        rows = await self.db._all("SELECT state,COUNT(*) AS n FROM maintenance_items WHERE preview_id=? GROUP BY state", (pid,))
        counts = {r["state"]: r["n"] for r in rows}
        await self.db.update_job(jid, state={**counts, "partial_failure": bool(counts.get("failed"))})

    async def retry(self, jid):
        job = await self.db.get_job(jid)
        if not job or job["kind"] != "metadata_apply" or job["status"] in ("running", "paused"):
            raise ValueError("仅能重试已停止的补全执行任务")
        if (await self.db.task_counts(jid)).get("running"):
            raise ValueError("任务仍在停止，请稍后重试")
        await self.db._write("UPDATE tasks SET status='pending',attempts=0,last_error='',updated_at=? WHERE job_id=?", (int(time.time()), jid))
        await self.db.update_job(jid, status="running", finished_at=None)
        self.e.notify()
