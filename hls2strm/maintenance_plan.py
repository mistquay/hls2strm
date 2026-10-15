"""维护操作的固定范围预览，影片组内保留完整物理副本清单。"""

from collections import Counter
import json
from pathlib import Path
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import aiosqlite

from .maintenance_files import RESOURCE_TYPES
from .maintenance_query import AssetFilter, FROM
from .maintenance_scan import decode_asset
from .storage import natural_key


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: AssetFilter = Field(default_factory=AssetFilter)
    action: Literal["resources", "remove", "reconcile"] = "resources"
    scope: Literal["library", "global"] = "library"
    resource_types: list[Literal["extra", "poster", "fanart", "thumb", "nfo", "trailer", "subtitle"]] = Field(default_factory=lambda: ["extra"])
    keep_extra: int = Field(3, ge=0, le=10000)
    disposition: Literal["trash", "permanent"] = "trash"
    retention_days: int = Field(7, ge=0, le=365)
    stop_collection: bool = True
    allow_empty: bool = False


def selected_files(asset, request):
    data = asset["data"]
    if request.action == "reconcile":
        return []
    if request.action == "remove":
        unknown = [f["path"] for f in data["files"] if f["kind"] not in RESOURCE_TYPES | {"strm"}]
        if unknown:
            raise ValueError("目录含未知文件、链接或实体媒体：" + "、".join(unknown[:5]))
        return data["files"]
    extra = {}
    for file in data["files"]:
        if file["kind"] == "extra":
            extra.setdefault(str(Path(file["path"]).parent), []).append(file)
    retain = set()
    for files in extra.values():
        files.sort(key=lambda f: (natural_key(Path(f["path"])), f["path"]))
        retain.update(f["path"] for f in files[:request.keep_extra])
    return [f for f in data["files"] if f["kind"] in request.resource_types and f["path"] not in retain]


async def _preview(manager, job, task, reader):
    db, e = manager.db, manager.e
    request = CleanupRequest.model_validate(job["params"]["request"])
    condition, args = request.query.where()
    await db._write("DELETE FROM cleanup_files WHERE item_id IN (SELECT id FROM maintenance_items WHERE preview_id=?)", (job["id"],))
    await db._write("DELETE FROM maintenance_items WHERE preview_id=?", (job["id"],))
    async with reader.execute("SELECT COALESCE(MAX(id),0) AS n FROM library_assets") as cur:
        maximum = (await cur.fetchone())["n"]
    cursor, seen, counts = 0, set(), Counter()
    checked_roots = {}
    config = manager.configuration()
    paths = await manager.find_paths(list(e.libs) if request.scope == "global" else request.query.library_ids or list(e.libs)) if request.action != "resources" else None
    while True:
        await e.checkpoint(job["id"])
        async with reader.execute(f"SELECT a.* FROM {FROM} WHERE {condition} AND a.id>? AND a.id<=? ORDER BY a.id LIMIT 100", (*args, cursor, maximum)) as cur:
            rows = await cur.fetchall()
        if not rows:
            break
        cursor = rows[-1]["id"]
        for raw in rows:
            await e.checkpoint(job["id"])
            asset = decode_asset(raw)
            vid = asset["video_id"]
            key = (vid or -asset["id"], 0 if request.scope == "global" else asset["library_id"])
            if request.action == "resources":
                key = (asset["id"], -1)
            if key in seen:
                continue
            seen.add(key)
            assets = [asset]
            error = ""
            try:
                if not vid:
                    raise ValueError("未纳管目录，需先确认影片关联")
                if request.scope == "global" and request.action == "resources":
                    raise ValueError("资源清理只作用于筛选的副本，请选择库级范围")
                if request.action != "resources":
                    cond, params = "a.video_id=?", [vid]
                    if request.scope == "library":
                        cond += " AND a.library_id=?"
                        params.append(asset["library_id"])
                    assets = [decode_asset(r) for r in await db._all(f"SELECT a.* FROM {FROM} WHERE a.scan_id=r.scan_id AND {cond}", params)]
                    expected_libs = list(e.libs) if request.scope == "global" else [asset["library_id"]]
                    root_key = tuple(sorted(expected_libs))
                    if root_key not in checked_roots:
                        try:
                            await manager.check_roots(expected_libs, allow_empty=request.allow_empty)
                            checked_roots[root_key] = ""
                        except (OSError, ValueError) as root_error:
                            checked_roots[root_key] = str(root_error)
                    if checked_roots[root_key]:
                        raise ValueError(checked_roots[root_key])
                else:
                    root = await db._one("SELECT health FROM maintenance_roots WHERE library_id=? AND root=?", (asset["library_id"], asset["root"]))
                    if not root or root["health"] not in ("ok", "empty"):
                        raise ValueError("目标目录不可达或尚未完整扫描")
                if any(a["status"] in ("conflict", "unmanaged", "cleaned") or a["nfo_status"] == "conflict" for a in assets):
                    raise ValueError("身份或 NFO 冲突，或副本已经清理，请先处理资料")
                if (request.query.source == "nfo" and manager.has_metadata_filter(request.query) and asset["nfo_status"] != "valid"
                        and not (request.query.include_unknown and asset["nfo_status"] == "missing")):
                    raise ValueError("筛选资料不是当前有效 NFO")
                if request.action == "reconcile":
                    if any(a["status"] == "present" for a in assets):
                        raise ValueError("相同范围内仍有影片副本，不能按缺失退库")
                elif any(a["status"] != "present" for a in assets):
                    raise ValueError("含缺失或未知副本，请先核对磁盘")
                if request.scope == "global":
                    for field in ("release_date", "categories", "tags"):
                        values = {json.dumps(a["metadata"].get(field), sort_keys=True, ensure_ascii=False)
                                  for a in assets if a["metadata"].get(field)}
                        if len(values) > 1:
                            raise ValueError("不同副本的 NFO 资料不一致，不能全局删除")
                for a in assets:
                    a["selected"] = selected_files(a, request)
                if request.action != "reconcile" and not any(a["selected"] for a in assets):
                    raise ValueError("没有符合本次清理类型或保留规则的文件")
                await manager.validate_assets(assets, request, paths=paths)
            except (OSError, ValueError) as exc:
                error = str(exc)
            video = await db.get_video_by_id(vid) if vid else None
            outputs = await db.get_outputs(vid) if vid else []
            data = {"assets": assets, "slug": video["slug"] if video else "", "code": video["code"] if video else "",
                    "title": asset["effective"].get("title") or (video["title"] if video else asset["path"]),
                    "identity": [video["code_key"], video["uncensored"]] if video else [],
                    "outputs": [dict(o) for o in outputs if request.scope == "global" or o["library_id"] == asset["library_id"]],
                    "metadata": asset["metadata"] if request.query.source == "nfo" else asset["effective"],
                    "field_sources": asset["data"].get("field_sources", {}), "request": request.model_dump()}
            async with db._tx(invalidate=False) as conn:
                cur = await conn.execute("INSERT INTO maintenance_items(preview_id,video_id,data,state,error) VALUES(?,?,?,?,?)",
                    (job["id"], vid, json.dumps(data, ensure_ascii=False), "skipped" if error else "planned", error))
                if not error:
                    files = {}
                    for a in assets:
                        directory = Path(a["root"]) / a["path"]
                        for f in a["selected"]:
                            source = str(directory / f["path"])
                            files[source] = (cur.lastrowid, source, json.dumps(f["fingerprint"]), f["kind"])
                    await conn.executemany("INSERT INTO cleanup_files(item_id,source,fingerprint,kind) VALUES(?,?,?,?)", list(files.values()))
                    counts["files"] += len(files)
                    counts["bytes"] += sum(json.loads(f[2])[2] for f in files.values())
            counts["checked"] += 1
            counts["skipped" if error else "eligible"] += 1
        await db.update_job(job["id"], state=dict(counts))
    if config != manager.configuration():
        raise ValueError("扫描预览期间库配置已变化，请重新预览")
    await db.update_job(job["id"], state={**dict(counts), "preview_token": secrets.token_urlsafe(24), "configuration": config,
                                          "request": request.model_dump(), "ready": True})


async def preview(manager, job, task):
    async with manager.db.snapshot_lock:
        async with aiosqlite.connect(manager.db.path, isolation_level=None) as reader:
            reader.row_factory = aiosqlite.Row
            await reader.execute("PRAGMA query_only=ON")
            await reader.execute("BEGIN")
            try:
                await _preview(manager, job, task, reader)
            finally:
                await reader.execute("ROLLBACK")
