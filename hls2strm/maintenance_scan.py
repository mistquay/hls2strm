"""分批扫描、本地 NFO 快照与成功扫描发布。失败批次不替换旧基线。"""

import asyncio
import datetime as dt
from collections import Counter
import json
from pathlib import Path
import time

from . import nfo
from .codes import code_key, edition
from .maintenance_files import walk, area_size
from .trash import TRASH_DIR
from .storage import safe_directory, next_batch


def source_metadata(video):
    if not video:
        return {}
    result = {k: video.get(k) for k in ("title", "release_date", "duration", "maker", "director", "series")}
    raw_date = result.get("release_date") or ""
    result["year"] = None
    try:
        result["release_date"] = dt.date.fromisoformat(raw_date).isoformat()
        result["year"] = int(result["release_date"][:4])
    except ValueError:
        result["release_date"] = ""
        if len(raw_date) == 4 and raw_date.isdigit() and 1000 <= int(raw_date) <= 9999:
            result["year"] = int(raw_date)
    for field in ("categories", "tags", "models"):
        values = video.get(field) or []
        if isinstance(values, str):
            values = json.loads(values)
        result[field] = [v["name"] for v in values if v.get("name")]
    return result


def read_metadata(root, data, video, previous, force):
    paths = [f for f in data["files"] if f["kind"] == "nfo" and Path(f["path"]).parent == Path(".")]
    same = {str(Path(s["path"]).with_suffix(".nfo")).casefold() for s in data["strms"] if not s["version"]}
    preferred = [f for f in paths if f["path"].casefold() in same]
    choices = preferred or [f for f in paths if f["path"].casefold() == "movie.nfo"] or paths
    data["nfo_path"] = choices[0]["path"] if len(choices) == 1 else ""
    metadata, status = {}, "missing"
    try:
        if len(choices) > 1:
            raise ValueError("多份 NFO，无法唯一确定资料")
        if choices:
            path = root / data["path"] / choices[0]["path"]
            if (not force and previous and previous["nfo_status"] == "valid"
                    and previous["data"].get("nfo_fingerprint") == choices[0]["fingerprint"]
                    and previous["data"].get("nfo_digest") == choices[0].get("sha256")
                    and previous["data"].get("nfo_version") == nfo.VERSION):
                metadata, stamp = previous["metadata"], choices[0]["fingerprint"]
            else:
                metadata, stamp = nfo.read(path)
            data.update(nfo_fingerprint=stamp, nfo_version=nfo.VERSION, nfo_digest=choices[0].get("sha256"))
            if video:
                for code in metadata.get("codes", []):
                    if code_key(code, video.get("maker", "")) != video["code_key"]:
                        raise ValueError("NFO 番号与 STRM 对应作品不一致")
                    # 普通 NFO 常常没有版本后缀，只拒绝明确声称 -U 却对应普通版的情况。
                    if edition(code, False, video.get("maker", "")) and not video["uncensored"]:
                        raise ValueError("NFO 版本与 STRM 对应作品不一致")
            status = "conflict" if metadata.get("errors") else "valid"
            data["nfo_error"] = "；".join(metadata.get("errors", []))
    except (OSError, ValueError) as error:
        status = "conflict" if "不一致" in str(error) or "多份" in str(error) else "invalid"
        data["nfo_error"] = str(error)
    if status != "valid" and previous and previous["metadata"]:
        data["historical_metadata"] = previous["metadata"]
    elif status != "valid" and previous and previous["data"].get("historical_metadata"):
        data["historical_metadata"] = previous["data"]["historical_metadata"]
    fallback = source_metadata(video)
    data["source_metadata"] = fallback
    effective = {**fallback, **{k: v for k, v in metadata.items() if v not in (None, "", [])}} if status == "valid" else fallback
    data["field_sources"] = {k: "nfo" if status == "valid" and metadata.get(k) not in (None, "", []) else "source"
                             for k in effective}
    return metadata, effective, status


def decode_asset(row):
    row = dict(row)
    for field in ("data", "metadata", "effective"):
        row[field] = json.loads(row[field])
    return row


async def save_asset(db, scan_id, library_id, root, data, metadata, effective, nfo_status):
    counts = Counter()
    for f in data["files"]:
        counts["bytes"] += f["size"]
        if f["kind"] == "extra":
            counts["extra_bytes"] += f["size"]
            counts["extra_count"] += 1
    async with db._tx(invalidate=False) as conn:
        cur = await conn.execute("""INSERT INTO library_assets(scan_id,library_id,root,path,video_id,status,nfo_status,
            metadata,effective,data,bytes,extra_bytes,extra_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scan_id, library_id, str(root), data["path"], data["video_id"], data["status"], nfo_status,
             json.dumps(metadata, ensure_ascii=False), json.dumps(effective, ensure_ascii=False),
             json.dumps(data, ensure_ascii=False), counts["bytes"], counts["extra_bytes"], counts["extra_count"]))
        asset_id = cur.lastrowid
        facets = [(asset_id, origin, field, value) for origin, values in (("nfo", metadata), ("effective", effective))
                  for field in ("categories", "tags", "models") for value in values.get(field, [])]
        await conn.executemany("INSERT OR IGNORE INTO asset_facets VALUES(?,?,?,?)", facets)
    return counts


async def scan(manager, job, task):
    e, db = manager.e, manager.db
    generation = job.get("_generation", job["id"])
    lid = int(task["target"])
    lib = e._library(lid)
    keys, cdn = dict((slug, vid) for vid, slug in await db.video_keys()), await db.cdn_video_map()
    total = Counter()
    for root in manager.roots(lib):
        await e.checkpoint(job["id"])
        previous_root = await db._one("SELECT * FROM maintenance_roots WHERE library_id=? AND root=?", (lid, str(root)))
        stats, found, has_strm = Counter(), set(), False
        try:
            root_id = await asyncio.to_thread(safe_directory, root)
            await db._write("DELETE FROM asset_facets WHERE asset_id IN (SELECT id FROM library_assets WHERE scan_id=? AND library_id=? AND root=?)", (generation, lid, str(root)))
            await db._write("DELETE FROM library_assets WHERE scan_id=? AND library_id=? AND root=?", (generation, lid, str(root)))
            iterator = walk(root, keys, cdn)
            try:
                while rows := await next_batch(iterator):
                    await e.checkpoint(job["id"])
                    for data in rows:
                        await e.checkpoint(job["id"])
                        data["root_identity"] = root_id
                        vid = data["video_id"]
                        video = await db.get_video_by_id(vid) if vid else None
                        old = await db._one("SELECT * FROM library_assets WHERE library_id=? AND root=? AND path=? AND scan_id!=? ORDER BY id DESC LIMIT 1",
                                            (lid, str(root), data["path"], generation))
                        old = decode_asset(old) if old else None
                        if old and old["data"].get("manual_binding") and not vid and data["status"] == "unmanaged":
                            vid = old["video_id"]
                            video = await db.get_video_by_id(vid)
                            data.update(video_id=vid, status="present", manual_binding=True)
                            data["strms"] = [{"path": f["path"], "video_id": vid, "version": False} for f in data["files"] if f["kind"] == "strm"]
                        metadata, effective, status = await asyncio.to_thread(read_metadata, root, data, video, old, job["params"].get("force", False))
                        if data.get("manual_binding") and status != "valid":
                            data["status"] = "conflict"
                        stats.update(await save_asset(db, generation, lid, root, data, metadata, effective, status))
                        stats["assets"] += 1
                        stats["files"] += len(data["files"])
                        stats["nfo_" + status] += 1
                        if vid and data["strms"]:
                            found.add(vid)
                        has_strm = has_strm or bool(data["strms"])
                    await db.update_job(job["id"], state={**dict(total + stats), "root": str(root)})
            finally:
                iterator.close()
            if root_id != await asyncio.to_thread(safe_directory, root):
                raise ValueError("扫描期间根目录身份改变")
            try:
                stats["trash_bytes"] = await asyncio.to_thread(area_size, root / TRASH_DIR)
            except (OSError, ValueError):
                stats["trash_unavailable"] = 1
            # 只由保存过输出路径的记录生成缺失项，待归库不参与。
            async for video, out in db.iter_outputs(lid):
                if not out["strm_path"] or video["id"] in found:
                    continue
                path = Path(out["strm_path"])
                if not path.is_relative_to(root):
                    continue
                missing_status = "conflict" if path.exists() else "missing"
                data = {"path": str(path.parent.relative_to(root)), "video_id": video["id"], "status": missing_status,
                        "files": [], "folders": [], "strms": [], "recorded_path": str(path), "root_identity": root_id}
                existing = await db._one("SELECT * FROM library_assets WHERE scan_id=? AND library_id=? AND root=? AND path=?",
                                         (generation, lid, str(root), data["path"]))
                if existing:
                    original_data = json.loads(existing["data"])
                    original_data["recorded_path"] = str(path)
                    local = json.loads(existing["metadata"])
                    if any(code_key(code, video.get("maker", "")) != video["code_key"] for code in local.get("codes", [])):
                        missing_status = "conflict"
                    await db._write("UPDATE library_assets SET video_id=?,status=?,data=? WHERE id=? AND video_id IS NULL",
                                    (video["id"], missing_status, json.dumps(original_data, ensure_ascii=False), existing["id"]))
                else:
                    await save_asset(db, generation, lid, root, data, {}, source_metadata(video), "missing")
                stats["missing"] += 1
            baseline = bool(has_strm or previous_root and previous_root["baseline"] and json.loads(previous_root["identity"]) == root_id)
            await db._write("""INSERT INTO maintenance_roots(library_id,root,scan_id,identity,health,error,checked_at,stats,baseline)
                VALUES(?,?,?,?,?,'',?,?,?) ON CONFLICT(library_id,root) DO UPDATE SET scan_id=excluded.scan_id,
                identity=excluded.identity,health=excluded.health,error='',checked_at=excluded.checked_at,stats=excluded.stats,baseline=excluded.baseline""",
                (lid, str(root), generation, json.dumps(root_id), "ok" if has_strm else "empty", int(time.time()), json.dumps(dict(stats)), int(baseline)))
            # 成功扫描才更新能找到的主文件位置；不调用会补多版本文件的 locate。
            rows = await db._all("SELECT video_id,root,path,data FROM library_assets WHERE scan_id=? AND library_id=? AND root=? AND status='present'",
                                 (generation, lid, str(root)))
            updates = []
            for row in rows:
                primaries = [s for s in json.loads(row["data"])["strms"] if not s["version"]]
                if primaries:
                    updates.append((str(root / row["path"] / primaries[0]["path"]), row["video_id"]))
            await db.set_output_paths(lid, updates)
        except (OSError, ValueError) as error:
            stats["failed_roots"] += 1
            await db._write("""INSERT INTO maintenance_roots(library_id,root,health,error,checked_at) VALUES(?,?,'unavailable',?,?)
                ON CONFLICT(library_id,root) DO UPDATE SET health='unavailable',error=excluded.error,checked_at=excluded.checked_at""",
                (lid, str(root), str(error), int(time.time())))
        total.update(stats)
    e.strm._index.pop(lid, None)
    await db.update_job(job["id"], state=dict(total))
    return total
