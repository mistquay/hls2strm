"""文件级持久化意图、可恢复回收与幂等执行。"""

import asyncio
import ctypes
import json
import os
from pathlib import Path
import stat
import time

from .maintenance_plan import CleanupRequest
from .storage import fingerprint, safe_directory
from .trash import TRASH_DIR, _is_link


def checked_parent(path, root, create=False):
    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
        raise ValueError("文件路径越过已确认根目录")
    safe_directory(root)
    parent = path.parent
    for part in reversed([parent, *parent.parents]):
        if not part.is_relative_to(root):
            continue
        if not part.exists() and create:
            part.mkdir()
        safe_directory(part)


def no_replace(source, destination):
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError("当前系统缺少不覆盖式移动接口，保留源文件")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(source))


def same_file(path, expected, moved=False):
    st = path.lstat()
    if _is_link(st) or not stat.S_ISREG(st.st_mode):
        return False
    actual = fingerprint(st)
    return actual[:4] == expected[:4] if moved else actual == expected


def change_file(row, root, restore=False):
    original = Path(row["source"])
    destination = Path(row["destination"]) if row["destination"] else None
    expected = json.loads(row["fingerprint"])
    checked_parent(original, root, create=restore)
    if destination:
        checked_parent(destination, root, create=not restore)
    if restore:
        if original.exists():
            if row["state"] == "restored" and same_file(original, expected, moved=True):
                return "restored"
            # 在移动恢复完成但日志提交前重启：目标不存在且原文件身份仍一致。
            if row["state"] == "restoring" and destination and not destination.exists() and same_file(original, expected, moved=True):
                return "restored"
            raise ValueError("原路径已有文件，未覆盖")
        if not destination or not same_file(destination, expected, moved=True):
            raise ValueError("回收文件已过期或变化，不能恢复")
        no_replace(destination, original)
        return "restored"
    if destination and destination.exists():
        if not original.exists() and same_file(destination, expected, moved=True):
            return "trashed"
        raise ValueError("回收目标冲突，保留两端")
    if not original.exists():
        return "absent"
    if not same_file(original, expected):
        raise ValueError("文件已改变，跳过")
    if destination:
        no_replace(original, destination)
        return "trashed"
    original.unlink()
    return "deleted"


async def thread_change(*args, archive_root=None, archive_identity=None, **kwargs):
    change = change_file
    if archive_root is not None:
        from .maintenance_archive import change_file as change
        kwargs["archive_root"] = Path(archive_root)
        kwargs["archive_identity"] = archive_identity
    worker = asyncio.create_task(asyncio.to_thread(change, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker  # 保证数据库关闭前本批文件操作已经停止。
        raise


def root_for(data, source):
    path = Path(source)
    roots = [Path(a["root"]) for a in data["assets"] if path.is_relative_to(Path(a["root"]) / a["path"])]
    if not roots:
        raise ValueError("文件不在冻结的副本范围内")
    return max(roots, key=lambda p: len(p.parts))


async def reserve(manager, job, item, data, request):
    db = manager.db
    vid = item["video_id"]
    libs = data["library_ids"] if request.action == "unavailable" else sorted({a["library_id"] for a in data["assets"]})
    scopes = [0] if request.scope == "global" else libs
    async with db._tx() as conn:
        if request.action != "resources":
            for lid in scopes:
                await conn.execute("INSERT OR IGNORE INTO collection_exclusions VALUES(?,?,?,?,?)",
                    (vid, lid, job["id"], "清理保护；" + ("主动删除" if request.stop_collection else "完成后允许补回"), int(time.time())))
        else:
            # 临时收录保护也用于只清资源，避免期间重新写入。
            for lid in libs:
                await conn.execute("INSERT OR IGNORE INTO collection_exclusions VALUES(?,?,?,?,?)",
                                   (vid, lid, job["id"], "资源清理进行中", int(time.time())))
        await conn.execute("UPDATE maintenance_items SET state='running',error='' WHERE id=?", (item["id"],))


async def finish_item(manager, job, item, data, request):
    db, vid = manager.db, item["video_id"]
    libs = set(data["library_ids"]) if request.action == "unavailable" else {a["library_id"] for a in data["assets"]}
    async with db._tx() as conn:
        for lid in libs:
            if request.action != "resources":
                await conn.execute("DELETE FROM outputs WHERE video_id=? AND library_id=?", (vid, lid))
                await conn.execute("UPDATE library_assets SET status='cleaned' WHERE video_id=? AND library_id=?", (vid, lid))
            else:
                for kind in request.resource_types:
                    await conn.execute("INSERT OR IGNORE INTO resource_exclusions VALUES(?,?,?,?)", (vid, lid, kind, job["id"]))
        if request.action == "resources" or not request.stop_collection:
            await conn.execute("DELETE FROM collection_exclusions WHERE video_id=? AND job_id=?", (vid, job["id"]))
        else:
            reason = "用户确认无可用源归档" if request.action == "unavailable" else "用户确认主动删除"
            await conn.execute("UPDATE collection_exclusions SET reason=? WHERE video_id=? AND job_id=?", (reason, vid, job["id"]))
        await conn.execute("UPDATE maintenance_items SET state='done',error='' WHERE id=?", (item["id"],))
    for lid in libs:
        manager.e.strm._index.pop(lid, None)


async def apply(manager, job, task):
    db, e = manager.db, manager.e
    preview = await db.get_job(job["params"]["preview_id"])
    if preview["state"]["configuration"] != manager.configuration():
        raise ValueError("库配置已变化，请重新预览；已完成项保留")
    request = CleanupRequest.model_validate(preview["params"]["request"])
    await manager.progress(job["id"], preview["id"])
    await manager.record_space(job["id"], preview["id"], "before")
    paths = await manager.find_paths(list(e.libs) if request.scope == "global" else request.query.library_ids or list(e.libs)) if request.action != "resources" else None
    cursor = 0
    while rows := await db._all("SELECT * FROM maintenance_items WHERE preview_id=? AND id>? ORDER BY id LIMIT 100", (preview["id"], cursor)):
        for item in rows:
            cursor = item["id"]
            await e.checkpoint(job["id"])
            if item["state"] in ("done", "skipped", "restored", "abandoned"):
                continue
            before = await manager.item_counts(item["id"])
            data = json.loads(item["data"])
            current_file = None
            try:
                async with e.file_changes.exclusive():
                    video = await db.get_video_by_id(item["video_id"])
                    if not video or [video["code_key"], video["uncensored"]] != data["identity"]:
                        raise ValueError("影片身份已变化，请重新预览")
                    await manager.validate_outputs(item, data)
                    files = [dict(r) for r in await db._all("SELECT * FROM cleanup_files WHERE item_id=? ORDER BY id", (item["id"],))]
                    resumed = item["state"] != "planned"
                    if request.action == "unavailable":
                        from .maintenance_unavailable import source_state, validate_item
                        if str(e.archive_root().absolute()) != data["archive_root"]:
                            raise ValueError("归档位置已变化，请重新预览")
                        if resumed:
                            if (await source_state(manager, item["video_id"]))[0]:
                                raise ValueError("影片已有本地可用源，停止后续归档")
                        else:
                            if any(Path(f["destination"]).exists() for f in files):
                                raise ValueError("归档目标已有文件，未处理该影片")
                            await validate_item(manager, data, paths)
                    elif not resumed:
                        await manager.validate_assets(data["assets"], request, paths=paths)
                    if resumed:
                        await manager.validate_resuming(data, files, request)
                    await reserve(manager, job, item, data, request)
                    for file in files:
                        await e.checkpoint(job["id"])
                        if file["state"] in ("trashed", "deleted", "absent"):
                            continue
                        root = root_for(data, file["source"])
                        if request.disposition == "trash" and request.retention_days:
                            file["destination"] = str(root / TRASH_DIR / "maintenance" / str(job["id"]) / Path(file["source"]).relative_to(root))
                        await db._write("UPDATE cleanup_files SET state='prepared',destination=?,error='' WHERE id=?", (file["destination"], file["id"]))
                        current_file = file["id"]
                        result = await thread_change(file, root, archive_root=data.get("archive_root"),
                                                     archive_identity=data.get("archive_identity"))
                        await db._write("UPDATE cleanup_files SET state=?,error='' WHERE id=?", (result, file["id"]))
                        current_file = None
                    await finish_item(manager, job, item, data, request)
                    if request.action == "unavailable":
                        from .maintenance_archive import remove_empty_folders
                        await asyncio.to_thread(remove_empty_folders, data["assets"])
            except (OSError, ValueError) as error:
                if current_file is not None:
                    await db._write("UPDATE cleanup_files SET error=? WHERE id=?", (str(error), current_file))
                await db._write("UPDATE maintenance_items SET state='failed',error=? WHERE id=?", (str(error), item["id"]))
            await manager.progress(job["id"], preview["id"], item["id"], before)
    await manager.progress(job["id"], preview["id"])
    await manager.record_space(job["id"], preview["id"], "after")
    if request.disposition != "archive":
        await db._write("UPDATE jobs SET params=json_set(params,'$.expires_at',?) WHERE id=?",
                        (int(time.time()) + request.retention_days * 86400, job["id"]))


async def restore_preview(manager, job, task):
    import secrets
    db = manager.db
    original = await db.get_job(job["params"]["restore_of"])
    if not original or original["kind"] != "maintenance_apply" or original["status"] in ("running", "paused"):
        raise ValueError("请先等待清理完成或取消清理任务")
    preview_id = original["params"]["preview_id"]
    await db._write("DELETE FROM maintenance_items WHERE preview_id=?", (job["id"],))
    count = 0
    cursor = 0
    while rows := await db._all("SELECT * FROM maintenance_items WHERE preview_id=? AND id>? ORDER BY id LIMIT 100", (preview_id, cursor)):
        for item in rows:
            cursor = item["id"]
            await manager.e.checkpoint(job["id"])
            if item["state"] == "restored":
                continue
            files = await db._all("SELECT * FROM cleanup_files WHERE item_id=? AND state IN ('trashed','restoring','restored')", (item["id"],))
            if not files:
                continue
            data = json.loads(item["data"])
            error = ""
            try:
                for f in files:
                    if f["state"] != "restored" and Path(f["source"]).exists():
                        raise ValueError("原路径已有文件，恢复会跳过此影片")
                    if f["state"] != "restored" and not Path(f["destination"]).is_file():
                        raise ValueError("回收文件已不存在")
            except (OSError, ValueError) as exc:
                error = str(exc)
            data.update(original_item=item["id"], restore_files=[f["id"] for f in files])
            await db._write("INSERT INTO maintenance_items(preview_id,video_id,data,state,error) VALUES(?,?,?,?,?)",
                            (job["id"], item["video_id"], json.dumps(data, ensure_ascii=False), "skipped" if error else "planned", error))
            count += not bool(error)
    await db.update_job(job["id"], state={"ready": True, "eligible": count, "configuration": manager.configuration(), "preview_token": secrets.token_urlsafe(24)})


async def restore(manager, job, task):
    db, e = manager.db, manager.e
    preview = await db.get_job(job["params"]["preview_id"])
    if preview["state"]["configuration"] != manager.configuration():
        raise ValueError("恢复预览后库配置改变，请重新预览")
    original_job_id = preview["params"]["restore_of"]
    original_job = await db.get_job(original_job_id)
    original_preview_id = original_job["params"]["preview_id"]
    await manager.progress(job["id"], preview["id"])
    cursor = 0
    while rows := await db._all("SELECT * FROM maintenance_items WHERE preview_id=? AND id>? ORDER BY id LIMIT 100", (preview["id"], cursor)):
        for item in rows:
            cursor = item["id"]
            await e.checkpoint(job["id"])
            if item["state"] in ("restored", "skipped"):
                continue
            before = await manager.item_counts(item["id"])
            data = json.loads(item["data"])
            try:
                async with e.file_changes.exclusive():
                    files = [dict(f) for f in await db._all("SELECT * FROM cleanup_files WHERE item_id=?", (data["original_item"],))
                             if f["id"] in data["restore_files"]]
                    if any(f["state"] not in ("trashed", "restoring", "restored") for f in files):
                        raise ValueError("恢复范围已改变")
                    for a in data["assets"]:
                        await manager.check_asset_root(a)
                    for f in files:
                        if f["state"] != "restored" and Path(f["source"]).exists() and not (
                                f["state"] == "restoring" and (data.get("archive_root") or not Path(f["destination"]).exists())):
                            raise ValueError("原路径冲突，保留两端")
                    for f in files:
                        await e.checkpoint(job["id"])
                        if f["state"] == "restored":
                            continue
                        f["state"] = "restoring"
                        await db._write("UPDATE cleanup_files SET state='restoring' WHERE id=?", (f["id"],))
                        if data.get("archive_root") and str(e.archive_root().absolute()) != data["archive_root"]:
                            raise ValueError("归档目录配置已变化，未恢复")
                        result = await thread_change(f, root_for(data, f["source"]), restore=True,
                                                     archive_root=data.get("archive_root"), archive_identity=data.get("archive_identity"))
                        await db._write("UPDATE cleanup_files SET state=? WHERE id=?", (result, f["id"]))
                    remaining = await db._one("SELECT 1 FROM cleanup_files WHERE item_id=? AND state!='restored' LIMIT 1", (data["original_item"],))
                    if remaining:
                        raise ValueError("存在不可恢复或未完成文件，已恢复的保留，禁收保护未解除")
                    async with db._tx() as conn:
                        await conn.execute("UPDATE maintenance_items SET state='restored',error='' WHERE id IN (?,?)", (item["id"], data["original_item"]))
                        for lid in data.get("library_ids", {a["library_id"] for a in data["assets"]}):
                            async with conn.execute("""SELECT 1 FROM maintenance_items other,json_each(other.data,'$.assets') a
                                WHERE other.preview_id=? AND other.video_id=? AND json_extract(a.value,'$.library_id')=?
                                AND other.state!='restored' AND EXISTS(SELECT 1 FROM cleanup_files f WHERE f.item_id=other.id
                                AND f.state IN ('prepared','trashed','deleted','absent','expired')) LIMIT 1""",
                                (original_preview_id, item["video_id"], lid)) as cursor_result:
                                pending = await cursor_result.fetchone()
                            if not pending:
                                await conn.execute("DELETE FROM resource_exclusions WHERE video_id=? AND library_id=? AND job_id=?", (item["video_id"], lid, original_job_id))
                                await conn.execute("DELETE FROM collection_exclusions WHERE video_id=? AND library_id=? AND job_id=?", (item["video_id"], lid, original_job_id))
                        if data["request"]["scope"] == "global":
                            await conn.execute("DELETE FROM collection_exclusions WHERE video_id=? AND library_id=0 AND job_id=?", (item["video_id"], original_job_id))
                        for out in data["outputs"]:
                            await conn.execute("INSERT OR IGNORE INTO outputs(video_id,library_id,strm_path,via) VALUES(?,?,?,?)",
                                               (item["video_id"], out["library_id"], out["strm_path"], out["via"]))
            except (OSError, ValueError) as error:
                await db._write("UPDATE maintenance_items SET state='failed',error=? WHERE id=?", (str(error), item["id"]))
            await manager.progress(job["id"], preview["id"], item["id"], before)
