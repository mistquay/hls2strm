"""无可用源及清退残留：直接核查磁盘，不依赖 outputs 或既有扫描索引。"""

import asyncio
from collections import Counter
import json
from pathlib import Path
import secrets

from .maintenance_archive import check_root, evidence
from .codes import code_key
from .maintenance_files import validate, walk
from .maintenance_plan import CleanupRequest
from .maintenance_scan import read_metadata
from .source_policy import eligible
from .storage import next_batch, safe_directory


async def source_state(manager, video_id):
    return (await source_states(manager, [video_id]))[video_id]


async def source_states(manager, video_ids):
    groups = await manager.db.sources_for(video_ids)
    lines = await manager.db.lines_for([s["id"] for sources in groups.values() for s in sources])
    retired = {r["site"] for r in await manager.db._all("SELECT site FROM site_retirements")}
    return {vid: (
        any(s["site"] not in retired and eligible(s, manager.e.store.current, lines.get(s["id"], [])) for s in groups.get(vid, [])),
        [{"site": s["site"], "status": s["status"], "retired": s["site"] in retired} for s in groups.get(vid, [])],
    ) for vid in video_ids}


def roots(manager, library_ids):
    result = []
    for lid in library_ids:
        lib = manager.e._library(lid)
        if lib.get("operation_pending"):
            raise ValueError("库配置有未完成变更")
        organized = manager.e.writer.external_root(lib)
        inbox = manager.e.writer.library_root(lib).absolute()
        result.extend((lid, root, root == inbox and organized is not None)
                      for root in dict.fromkeys([*manager.roots(lib), inbox]))
    return result


def split_inbox(data):
    """平铺收件区只认 STRM 内容，不把共享的根目录元数据归给某一部片。"""
    if data["path"] != ".":
        return [data]
    groups = {}
    for entry in data["strms"]:
        if Path(entry["path"]).parent == Path("."):
            groups.setdefault(entry["video_id"], []).append(entry)
    result = []
    for vid, strms in groups.items():
        names = {s["path"] for s in strms}
        result.append({**data, "video_id": vid, "status": "present", "strms": strms,
                       "files": [f for f in data["files"] if f["path"] in names],
                       "folders": data["folders"][:1], "partial": True, "error": ""})
    return result


async def metadata_identity(manager, root, data):
    if data["video_id"] or data["path"] == "." or any(f["kind"] == "strm" for f in data["files"]):
        return data
    metadata, _, status = await asyncio.to_thread(read_metadata, root, data, None, None, False)
    keys = {code_key(code, metadata.get("maker", "")) for code in metadata.get("codes", [])}
    if status != "valid" or not keys:
        return data
    rows = await manager.db._all("SELECT id FROM videos WHERE code_key IN (SELECT value FROM json_each(?))",
                                 (json.dumps(sorted(keys)),))
    if len(rows) == 1:
        return {**data, "video_id": rows[0]["id"], "status": "present", "metadata_only": True}
    return data


async def validate_item(manager, data, paths=None):
    await asyncio.to_thread(check_root, Path(data["archive_root"]), identity=data["archive_identity"])
    usable, _ = await source_state(manager, data["video_id"])
    if usable:
        raise ValueError("影片已有本地可用源，未归档，请重新核查")
    paths = paths if paths is not None else await manager.find_paths(data["library_ids"])
    for lid in data["library_ids"]:
        if isinstance(paths.get(lid), str):
            raise ValueError(paths[lid])
        expected = {str(Path(a["root"]) / a["path"] / f["path"]) for a in data["assets"] if a["library_id"] == lid
                    for f in a["data"]["strms"]}
        if paths.get(lid, {}).get(data["video_id"], set()) != expected:
            raise ValueError("影片副本范围已变化，请重新核查")
        for out in data["outputs"]:
            if out["library_id"] == lid and out["strm_path"] and Path(out["strm_path"]).exists() and out["strm_path"] not in expected:
                raise ValueError("记录的输出不在本次可识别副本中，未归档")
    for a in data["assets"]:
        await manager.check_asset_root(a)
        await asyncio.to_thread(validate, Path(a["root"]), a["data"], strict=not a["data"].get("partial"))
        for f in a["selected"]:
            await asyncio.to_thread(evidence, Path(a["root"]) / a["path"] / f["path"], f["fingerprint"])


async def preview(manager, job):
    db, e = manager.db, manager.e
    request = CleanupRequest.model_validate(job["params"]["request"])
    config = manager.configuration()
    archive_root = e.archive_root().absolute()
    archive_identity = await asyncio.to_thread(check_root, archive_root)
    lids = sorted(set(request.query.library_ids or e.libs))
    scan_roots = roots(manager, lids)
    await db.update_job(job["id"], state={"scan_phase": "preparing", "archive_root": str(archive_root)})
    keys = {slug: vid for vid, slug in await db.video_keys()}
    cdn = await db.cdn_video_map()
    groups, states, counts = {}, {}, Counter()
    await db._write("DELETE FROM cleanup_files WHERE item_id IN (SELECT id FROM maintenance_items WHERE preview_id=?)", (job["id"],))
    await db._write("DELETE FROM maintenance_items WHERE preview_id=?", (job["id"],))
    for lid, root, inbox in scan_roots:
        await db.update_job(job["id"], state={**dict(counts), "scan_phase": "walking", "root": str(root), "archive_root": str(archive_root)})
        identity = await asyncio.to_thread(safe_directory, root)
        iterator = walk(root, keys, cdn)
        try:
            while batch := await next_batch(iterator):
                await e.checkpoint(job["id"])
                prepared = []
                for raw in batch:
                    raw = await metadata_identity(manager, root, raw)
                    prepared.extend(split_inbox(raw) if inbox else [raw])
                needed = {s["video_id"] for data in prepared for s in data["strms"]}
                needed.update(data["video_id"] for data in prepared if data["video_id"])
                if request.query.video_ids:
                    needed.intersection_update(request.query.video_ids)
                if needed - states.keys():
                    states.update(await source_states(manager, sorted(needed - states.keys())))
                for data in prepared:
                    vids = {s["video_id"] for s in data["strms"]}
                    if data["video_id"]:
                        vids.add(data["video_id"])
                    if not vids:
                        counts["unknown"] += 1
                        unknown = {"id": -counts["unknown"], "library_id": lid, "root": str(root),
                                   "path": data["path"], "nfo_status": "missing", "data": data}
                        await db._write("INSERT INTO maintenance_items(preview_id,data,state,error) VALUES(?,?,'skipped',?)",
                            (job["id"], json.dumps({"code": "", "slug": "", "title": str(root / data["path"]),
                                "assets": [unknown], "metadata": {}, "field_sources": {}}, ensure_ascii=False),
                             "未识别影片或 NFO 无法唯一关联，保留目录"))
                        counts["skipped"] += 1
                        counts["checked"] += 1
                    for vid in vids:
                        if request.query.video_ids and vid not in request.query.video_ids:
                            continue
                        if states[vid][0]:
                            continue
                        video = await db.get_video_by_id(vid)
                        data = {**data, "root_identity": identity, "inbox": inbox}
                        metadata, effective, status = await asyncio.to_thread(read_metadata, root, data, video, None, False)
                        error = ""
                        if data["status"] != "present" or status == "conflict":
                            error = "共享目录、未知 STRM 或 NFO 身份冲突，整部影片保留"
                        if data["path"] == "." and not data.get("partial"):
                            error = "影片直接位于库根目录，无法独占归属，未归档"
                        if any(f["kind"] in ("protected", "media") for f in data["files"]):
                            error = "目录含链接、特殊文件或实体媒体，整部影片保留"
                        asset = {"id": -counts["assets"] - 1, "library_id": lid, "video_id": vid,
                                 "root": str(root), "path": data["path"], "status": data["status"],
                                 "nfo_status": status, "metadata": metadata, "effective": effective,
                                 "data": data, "selected": data["files"]}
                        group = groups.setdefault(vid, {"video": video, "assets": [], "error": ""})
                        group["assets"].append(asset)
                        group["error"] = group["error"] or error
                        counts["assets"] += 1
                    counts["directories"] += 1
                await db.update_job(job["id"], state={**dict(counts), "scan_phase": "walking", "root": str(root), "archive_root": str(archive_root)})
        finally:
            iterator.close()
        if identity != await asyncio.to_thread(safe_directory, root):
            raise ValueError("核查期间根目录身份改变，请检查挂载")
    paths = await manager.find_paths(lids)
    for vid, group in sorted(groups.items()):
        await e.checkpoint(job["id"])
        video, assets, error = group["video"], group["assets"], group["error"]
        outputs = [dict(o) for o in await db.get_outputs(vid) if o["library_id"] in lids]
        data = {"video_id": vid, "library_ids": lids, "assets": assets, "outputs": outputs,
                "identity": [video["code_key"], video["uncensored"]], "code": video["code"],
                "slug": video["slug"], "title": video["title"], "metadata": assets[0]["metadata"],
                "field_sources": assets[0]["data"].get("field_sources", {}), "sources": states[vid][1],
                "reason": "清退残留且无可用源" if any(s["retired"] for s in states[vid][1]) else "没有本地启用且支持的可用源",
                "archive_root": str(archive_root), "archive_identity": archive_identity, "request": request.model_dump()}
        files = {}
        if not error:
            try:
                await validate_item(manager, data, paths)
                for a in assets:
                    area = "未刮削" if a["data"].get("inbox") or e.writer.external_root(e.libs[a["library_id"]]) is None else "已刮削"
                    dest = archive_root / "maintenance" / str(job["id"]) / f"library-{a['library_id']}" / area
                    for f in a["selected"]:
                        source = Path(a["root"]) / a["path"] / f["path"]
                        stamp = await asyncio.to_thread(evidence, source, f["fingerprint"])
                        files[str(source)] = (str(dest / a["path"] / f["path"]), stamp, f["kind"])
            except (OSError, ValueError) as exc:
                error = str(exc)
        async with db._tx(invalidate=False) as conn:
            cur = await conn.execute("INSERT INTO maintenance_items(preview_id,video_id,data,state,error) VALUES(?,?,?,?,?)",
                (job["id"], vid, json.dumps(data, ensure_ascii=False), "skipped" if error else "planned", error))
            if not error:
                await conn.executemany("INSERT INTO cleanup_files(item_id,source,destination,fingerprint,kind) VALUES(?,?,?,?,?)",
                    [(cur.lastrowid, src, dst, json.dumps(stamp), kind) for src, (dst, stamp, kind) in files.items()])
                counts["files"] += len(files)
                counts["bytes"] += sum(stamp[2] for _, stamp, _ in files.values())
        counts["checked"] += 1
        counts["skipped" if error else "eligible"] += 1
        await db.update_job(job["id"], state={**dict(counts), "scan_phase": "planning", "archive_root": str(archive_root)})
    if config != manager.configuration():
        raise ValueError("核查期间库配置已变化，请重新核查")
    await db.update_job(job["id"], state={**dict(counts), "ready": True, "configuration": config,
        "kept": sum(usable for usable, _ in states.values()),
        "archive_root": str(archive_root), "preview_token": secrets.token_urlsafe(24)})
