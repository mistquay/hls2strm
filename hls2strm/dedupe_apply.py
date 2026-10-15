"""按已确认的组清文件、合并数据库、重写输出；各断点可恢复。"""

import asyncio
import hashlib
import json
import logging
import os
import time

from .codes import edition
from .dedupe import metadata_empty, output_state, video_state
from .operation_plans import context, digest
from .operations import ScopeChanged

log = logging.getLogger(__name__)


async def save_item(conn, preview_id, item):
    await conn.execute("UPDATE operation_items SET data=? WHERE preview_id=? AND video_id=?",
                       (json.dumps(item, ensure_ascii=False), preview_id, item["group_id"]))


async def validate(engine, item, *, full=True):
    """full：清文件之前全面核对。开始清文件以后只核对还是不是同一组（影片都在，键和版本没变）：
    源、输出、状态之后再变不影响合并，不能因此把组停在文件已清、库没合并的半截。"""
    for expected in item["input"]:
        vid = expected["video"]["id"]
        video = await engine.db.get_video_by_id(vid)
        if not full:
            if not video or any(video[k] != expected["video"][k] for k in ("code_key", "uncensored")):
                raise ScopeChanged(f"组 #{item['group_id']} 的影片已删除或番号、版本已改变，请重新预览")
            continue
        if not video or video_state(video) != video_state(expected["video"]):
            raise ScopeChanged(f"组 #{item['group_id']} 的影片信息已改变，请重新预览；已完成的组保留")
        sources = [{k: s[k] for k in ("id", "site", "key", "status")} for s in await engine.db.get_sources(vid)]
        if sources != expected["sources"]:
            raise ScopeChanged(f"影片 {video['slug']} 的源已改变，请重新预览")
        actual = output_state(await engine.db.get_outputs(vid))
        old = expected["outputs"]
        cleaning = item.get("cleaning", [])
        allowed_missing = {lid for member, lid in cleaning if member == vid}
        if any(o not in old for o in actual) or any(o not in actual and o["library_id"] not in allowed_missing for o in old):
            raise ScopeChanged(f"影片 {video['slug']} 的输出归属或路径已改变，请重新预览")


async def merge_crawl(conn, loser, keep):
    # 同任务只能有一条作品明细：保留已有文本，合并识别/加入结果，不伪造新 slug。
    async with conn.execute("SELECT * FROM crawl_items WHERE video_id=?", (loser,)) as cur:
        rows = await cur.fetchall()
    for row in rows:
        cur = await conn.execute("""UPDATE crawl_items SET is_new=MAX(is_new,?),added=MAX(added,?),excluded=MIN(excluded,?)
                                    WHERE job_id=? AND video_id=?""",
                                 (row["is_new"], row["added"], row["excluded"], row["job_id"], keep))
        if cur.rowcount:
            log.info("同片整理：任务 #%s 的重复识别明细 %s 并入影片 #%s，保留已有明细文本", row["job_id"], row["slug"], keep)
            await conn.execute("DELETE FROM crawl_items WHERE job_id=? AND video_id=?", (row["job_id"], loser))
    await conn.execute("UPDATE crawl_items SET video_id=? WHERE video_id=?", (keep, loser))


async def merge(engine, preview_id, job_id, item):
    db, keep = engine.db, item["keep"]
    async with db._tx() as conn:
        await validate(engine, item, full=False)
        video = await db.get_video_by_id(keep)
        fields = {}
        metadata = ("title", "duration", "thumb_url", "preview_url", "cover_url", "views", "likes", "favs",
                    "release_date", "quality", "models", "categories", "tags", "maker", "director", "series", "detail_at")
        for member in item["members"]:
            loser = member["id"]
            if loser == keep:
                continue
            current_member = await db.get_video_by_id(loser)  # 详情可能刚刷新，只从最新数据补空字段。
            for key in metadata:
                if metadata_empty(key, video.get(key)) and not metadata_empty(key, current_member.get(key)):
                    video[key] = current_member[key]
                    fields[key] = json.dumps(current_member[key], ensure_ascii=False) if key in ("models", "categories", "tags") else current_member[key]
            from .collection_policy import merge as merge_policy
            await merge_policy(conn, loser, keep)
            await conn.execute("UPDATE sources SET video_id=? WHERE video_id=?", (keep, loser))
            # source_lines 通过 source_id 关联，不改源 ID，因此线路和 removed 状态都原样保留。
            await conn.execute("""INSERT INTO source_checks(video_id,site,found,checked_at)
                                  SELECT ?,site,found,checked_at FROM source_checks WHERE video_id=?
                                  ON CONFLICT(video_id,site) DO UPDATE SET
                                  found=MAX(found,excluded.found),checked_at=MAX(checked_at,excluded.checked_at)""", (keep, loser))
            await conn.execute("DELETE FROM source_checks WHERE video_id=?", (loser,))
            for out in member["outputs"]:
                await conn.execute("INSERT OR IGNORE INTO outputs(video_id,library_id,via) VALUES(?,?,?)",
                                   (keep, out["library_id"], out["via"]))
            await conn.execute("UPDATE play_history SET video_id=? WHERE video_id=?", (keep, loser))
            await conn.execute("UPDATE play_history SET served_video_id=? WHERE served_video_id=?", (keep, loser))
            await merge_crawl(conn, loser, keep)
            for archive in await db._all("SELECT moves FROM archives WHERE video_id=?", (loser,)):
                log.info("同片整理 %s：归档文件原样保留 %s", member["slug"], archive["moves"])
            await conn.execute("DELETE FROM archives WHERE video_id=?", (loser,))
            await conn.execute("UPDATE video_aliases SET video_id=? WHERE video_id=?", (keep, loser))
            await conn.execute("INSERT INTO video_aliases(slug,video_id,created_at,job_id) VALUES(?,?,?,?)",
                               (member["slug"], keep, int(time.time()), job_id))
            await conn.execute("DELETE FROM videos WHERE id=?", (loser,))
        if not edition(video["code"], video["uncensored"], video["maker"]):
            fields["uncensored"] = 0
        if fields:
            await conn.execute(f"UPDATE videos SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), keep))
        item["merged"] = True
        await save_item(conn, preview_id, item)  # 合并和断点同事务，重试不会拿别名再清文件。


async def apply_group(engine, preview_id, job_id, item):
    db = engine.db
    if not item.get("merged"):
        await validate(engine, item, full=not item.get("cleaning"))  # 中断后续做：已经开始清文件就只核对身份
        for member in item["members"]:
            if member["id"] == item["keep"]:
                continue
            for out in member["outputs"]:
                await engine.checkpoint(job_id)
                pair = [member["id"], out["library_id"]]
                if pair not in item.setdefault("cleaning", []):
                    item["cleaning"].append(pair)
                    async with db._tx() as conn:
                        await save_item(conn, preview_id, item)
                lib = engine.libs[out["library_id"]]
                if lib["external_dir"]:
                    indexed = (await engine.strm.index(lib)).get(member["id"], [])
                    if indexed and not await asyncio.to_thread(lambda: all(os.path.isfile(p) for p in indexed)):
                        await engine.strm.index(lib, max_age=0)
                await engine._remove_files(lib, member["id"], member["slug"], out["strm_path"])
                await db.delete_output(member["id"], lib["id"])
        await merge(engine, preview_id, job_id, item)
    video = await db.get_video_by_id(item["keep"])
    if video is None:
        raise ScopeChanged("保留影片已不存在，请核对本组")
    # 保留者可能在预览后被 MDCNG 移动，按现有内容索引找回再原地重写。
    for out in await db.get_outputs(video["id"]):
        lib = engine.libs[out["library_id"]]
        if lib["external_dir"] and out["strm_path"]:
            paths = await engine.strm.paths_of(lib, video["id"], out["strm_path"])
            if paths:
                await db.set_output(video["id"], lib["id"], engine.strm.pick(lib, paths))
    await engine._output_all(video, cover=False)
    await engine.apply_rules(video, cover=False)
    engine._settle_wanted = True
    for missing in item["missing"]:
        log.warning("同片整理 %s：未找到输出，保留旧链接兼容 %s", missing["slug"], missing["path"])
    for folder in item["shared_dirs"]:
        log.info("同片整理：共用目录只清 STRM，其他元数据保留 %s", folder)


async def apply(engine, job, preview):
    request, db = preview["params"]["operation"], engine.db
    async with engine.file_changes.exclusive():
        state = dict((await db.get_job(job["id"]))["state"])
        if state.get("completed"):
            return _result(state)
        if digest(context(engine, request)) != digest(preview["state"]["_context"]):
            raise ScopeChanged("同片整理的配置已改变，请重新预览；已完成的组保留")
        if not state.get("started"):
            # 核对已确认预览自身的指纹；当前影片变化逐组处理，不重算整批保留项。
            fingerprint = hashlib.sha256(digest([preview["state"]["_context"], request]).encode())
            after = -1
            while True:
                rows = await db._all("SELECT video_id,data FROM operation_items WHERE preview_id=? AND video_id>? ORDER BY video_id LIMIT 100",
                                     (preview["id"], after), bulk=True)
                if not rows:
                    break
                await engine.checkpoint(job["id"])
                for row in rows:
                    item = json.loads(row["data"])
                    fingerprint.update(digest([item["group_id"], item["input"], item["keep"], item["selected"]]).encode())
                after = rows[-1]["video_id"]
            if fingerprint.hexdigest() != preview["state"]["_scope"]:
                raise ScopeChanged("已确认的同片整理预览记录已改变，请重新预览")
            state["started"] = True
            await db.update_job(job["id"], state=state)
        cursor = state.get("cursor", -1)
        engine.strm._index.clear()  # 执行或恢复时每座库重新定位一次，不逐组扫描全库。
        while True:
            rows = await db._all("SELECT video_id,data FROM operation_items WHERE preview_id=? AND video_id>? ORDER BY video_id LIMIT 100",
                                 (preview["id"], cursor), bulk=True)
            if not rows:
                break
            for row in rows:
                await engine.checkpoint(job["id"])
                item = json.loads(row["data"])
                if item["selected"]:
                    try:
                        await apply_group(engine, preview["id"], job["id"], item)
                    except ScopeChanged as error:
                        reason = str(error)
                        if item.get("cleaning") and not item.get("merged"):
                            reason += "；并入影片的文件已清理，重新预览后可完成合并"
                        state.setdefault("skipped_groups", []).append(
                            {"group_id": item["group_id"], "code_key": item["code_key"], "reason": reason})
                        state["skipped"] = state.get("skipped", 0) + 1
                        log.warning("同片整理：跳过组 #%s（%s）：%s", item["group_id"], item["code_key"], reason)
                    else:
                        state["processed"] = state.get("processed", 0) + 1
                cursor = row["video_id"]
                state["cursor"] = cursor
                await db.update_job(job["id"], state=state)
        state["completed"] = True
        engine.strm._index.clear()  # 下次索引纳入本次新建的别名。
        await db.update_job(job["id"], state=state)
        return _result(state)


def _result(state):
    return {"merged_groups": state.get("processed", 0), "skipped_groups": state.get("skipped", 0)}
