"""一次性前缀批次：沿用引擎队列、站点限速、文件保护和重试。"""

import logging
import asyncio
import time

from .codes import code_key, series_prefix
from .operation_plans import digest
from .prefix_store import matching_video, normalize_prefixes, record_skip
from .sites import SITES, get_site

log = logging.getLogger(__name__)


async def create(engine, body):
    db = engine.db
    scope = await db._one("SELECT fingerprint FROM prefix_scopes WHERE id=? AND ready=1", (body["scope_id"],))
    if scope is None:
        raise ValueError("统计快照尚未完成或已过期，请重新统计")
    prefixes = normalize_prefixes(body["prefixes"])
    sites = sorted(set(body["sites"]))
    for site in sites:
        st = get_site(site)
        if not st.keyword_source or not engine.store.current.site(site).enabled:
            raise ValueError(f"{st.label} 未启用或不支持前缀搜索")
    lib_id = body["library_id"] if body["mode"] == "expand" else None
    if lib_id:
        lib = engine._library(lib_id)
        if lib.get("operation_pending"):
            raise ValueError("输出库有待处理变更，请先完成变更任务")
    if body["mode"] == "expand" and not lib_id:
        raise ValueError("收录新片时请选择输出库")
    params = {**body, "prefixes": prefixes, "sites": sites, "library_id": lib_id}
    signature = digest({**params, "scope_id": scope["fingerprint"]})
    params["signature"] = signature
    async with engine._claim_lock:
        existing = await db._one("SELECT id FROM jobs WHERE kind='prefix_crawl' AND status IN ('running','paused') AND json_extract(params,'$.signature')=?", (signature,))
        initialized = existing and (await db._one("SELECT COUNT(*) AS n FROM prefix_runs WHERE job_id=?", (existing["id"],)))["n"] == len(prefixes) * len(sites)
        if initialized:
            from .request_jobs import current_request
            if request := current_request.get():
                await db._write("INSERT OR IGNORE INTO operation_requests(key,digest,job_id) VALUES(?,?,?)", (*request, existing["id"]))
            return existing["id"]
        active = await db._all(
            """SELECT j.id,r.site,r.prefix FROM prefix_runs r JOIN jobs j ON j.id=r.job_id JOIN prefix_scopes sc ON sc.id=json_extract(j.params,'$.scope_id')
               WHERE j.status IN ('running','paused') AND r.cancelled=0
               AND sc.fingerprint=? AND json_extract(j.params,'$.mode')=?
               AND COALESCE(json_extract(j.params,'$.library_id'),0)=?""",
            (scope["fingerprint"], body["mode"], lib_id or 0))
        for duplicate in active:
            if (not existing or duplicate["id"] != existing["id"]) and duplicate["site"] in sites and duplicate["prefix"] in prefixes:
                raise ValueError(f"{SITES[duplicate['site']].label} / {duplicate['prefix']} 已有进行中的任务 #{duplicate['id']}")
        # 初始化期间暂不领取；中断后同一提交标识可以继续补齐计划。
        jid = existing["id"] if existing else await db.create_job("prefix_crawl", f"按前缀抓取：{len(prefixes)} 个前缀 → " + "、".join(SITES[s].label for s in sites), params, status="paused")
        planned = [(site, prefix, SITES[site].prefix_source(prefix)) for site in sites for prefix in prefixes]
        for start in range(0, len(planned), 256):
            batch = planned[start:start + 256]
            async with db._tx(invalidate=False) as conn:
                await conn.executemany("INSERT OR IGNORE INTO prefix_runs(job_id,site,prefix,source) VALUES(?,?,?,?)",
                                       [(jid, site, prefix, source) for site, prefix, source in batch])
                await conn.executemany("INSERT OR IGNORE INTO tasks(job_id,kind,target,priority,status,site,updated_at) VALUES(?,'prefix_list',?,10,'pending',?,?)",
                                       [(jid, f"{site}/{prefix}/1", site, int(time.time())) for site, prefix, _ in batch])
            await asyncio.sleep(0)
        await db._write("UPDATE jobs SET status='running' WHERE id=? AND status='paused'", (jid,), invalidate=False)
        db.job_cache.clear()
    engine.notify()
    return jid


async def checkpoint(engine, job_id, site, prefix):
    from .engine import TaskStopped
    await engine.checkpoint(job_id)
    run = await engine.db._one("SELECT * FROM prefix_runs WHERE job_id=? AND site=? AND prefix=?", (job_id, site, prefix))
    if run is None or run["cancelled"]:
        raise TaskStopped("cancelled")
    return run


async def do_list(engine, job, task):
    db, params = engine.db, job["params"]
    site_name, prefix, page_text = task["target"].split("/", 2)
    page = int(page_text)
    run = await checkpoint(engine, job["id"], site_name, prefix)
    site = get_site(site_name)
    from .errors import NotFound, ParseError
    try:
        fetched = await engine.fetcher.site(site_name).get_page(site.page_url(run["source"], page, site.default_sort, run["block_id"]))
    except NotFound as error:
        raise ParseError(f"搜索列表不存在：{error}；可检查站点后重试") from error
    try:
        listing = site.parse_list(fetched.html)
        if not listing.items:
            # 沿用列表解析的保守行为：无法确认正常空搜索页时不能报完整扫描。
            raise ParseError("搜索列表为空，无法确认是没有结果还是页面异常；请检查列表快照")
    except Exception as error:
        error.html = fetched.html
        raise
    failures = []
    for item in listing.items:
        await checkpoint(engine, job["id"], site_name, prefix)
        try:
            await ingest(engine, job, task, site, prefix, page, item)
        except Exception as error:
            from .engine import TaskStopped
            if isinstance(error, TaskStopped):
                raise
            failures.append(f"{item.key}: {error}")
            log.error("前缀 %s / %s 第 %d 页：%s 入库失败", site.label, prefix, page, item.key, exc_info=True)
    if failures:
        raise RuntimeError("；".join(failures[:5]))
    await checkpoint(engine, job["id"], site_name, prefix)
    last_page = listing.last_page or page
    limit = params["max_pages"]
    end = min(last_page, limit) if limit else last_page
    # 下一页与扫描完成状态原子落库；崩溃重跑不会漏页或重复排队。
    async with db._tx(invalidate=False) as conn:
        current = await db._one("SELECT cancelled FROM prefix_runs WHERE job_id=? AND site=? AND prefix=?", (job["id"], site_name, prefix))
        if not current["cancelled"]:
            await conn.execute("UPDATE prefix_runs SET block_id=?,last_page=?,limited=?,listing_complete=? WHERE job_id=? AND site=? AND prefix=?",
                               (listing.block_id or run["block_id"], last_page, int(bool(limit and last_page > limit)), int(page >= end), job["id"], site_name, prefix))
            if page < end:
                await conn.execute("INSERT OR IGNORE INTO tasks(job_id,kind,target,priority,status,site,updated_at) VALUES(?,'prefix_list',?,10,'pending',?,?)",
                                   (job["id"], f"{site_name}/{prefix}/{page + 1}", site_name, int(time.time())))
    log.info("前缀 %s / %s：第 %d/%d 页，识别 %d 条%s", site.label, prefix, page, last_page, len(listing.items), "（达到页数上限）" if limit and page >= limit and last_page > limit else "")
    engine.notify()


async def ingest(engine, job, task, site, prefix, page, item):
    db, params = engine.db, job["params"]
    if params["strict"] and series_prefix(code_key(item.code)) != prefix:
        await record_skip(db, job["id"], site.name, prefix, page, item, "mismatch")
        return
    scope_id = params["scope_id"] if params["mode"] == "existing" else None
    existing = await matching_video(db, site.name, item, scope_id)
    if scope_id and existing is None:
        await record_skip(db, job["id"], site.name, prefix, page, item, "outside")
        return
    vid, _ = await db.upsert_item(site.name, item, engine._slug_for(site, item.key, item.code, item.uncensored),
                                  engine._rank, video_id=existing["id"] if existing else None,
                                  crawl=(job["id"], task["id"], page), prefix_hit=(job["id"], prefix, page))
    video = await db.get_video_by_id(vid)
    source = await db.find_source(site.name, item.key)
    hit = await db._one("SELECT * FROM prefix_hits WHERE job_id=? AND site=? AND source_key=?", (job["id"], site.name, item.key))
    if source["status"] == "removed" or video["status"] == "removed":
        return
    if hit["new_video"] and params["mode"] == "expand":
        lib = engine._library(params["library_id"])
        if await db.in_libraries(vid, lib["excludes"]):
            await db.history.output_result(job["id"], vid, excluded=True)
            await db._write("UPDATE prefix_hits SET outcome='excluded' WHERE job_id=? AND site=? AND source_key=?", (job["id"], site.name, item.key), invalidate=False)
            return
        await db.ensure_output(vid, lib["id"], crawl_job=job["id"])
        await engine._output_one(video, lib["id"], cover=False)
    else:
        await engine._output_all(video, cover=False)
    if params["detail"] and source["detail_at"] is None:
        await db.add_tasks(job["id"], "prefix_detail", [f"{site.name}/{prefix}/{item.key}"], 0, site.name)


async def do_detail(engine, job, task):
    site, prefix, key = task["target"].split("/", 2)
    await checkpoint(engine, job["id"], site, prefix)
    source = await engine.db.find_source(site, key)
    if source and source["status"] == "removed":
        return
    # 不传 library_id：已有影片不改变归属；新片已在列表阶段加入输出库。
    await engine.fetch_detail(site, key, job_id=job["id"])


async def cancel(engine, job_id, site, prefix):
    async with engine._claim_lock:
        async with engine.db._tx(invalidate=False) as conn:
            job = await engine.db.get_job(job_id)
            if job is None or job["kind"] != "prefix_crawl":
                raise ValueError("前缀批次不存在")
            if job["status"] not in ("running", "paused"):
                raise ValueError("批次已结束")
            cur = await conn.execute("UPDATE prefix_runs SET cancelled=1 WHERE job_id=? AND site=? AND prefix=?", (job_id, site, prefix))
            if not cur.rowcount:
                raise ValueError("前缀任务不存在")
            await conn.execute("UPDATE tasks SET status='cancelled',last_error='用户取消此站点前缀',updated_at=? WHERE job_id=? AND site=? AND kind IN ('prefix_list','prefix_detail') AND target LIKE ? AND status='pending'",
                               (int(time.time()), job_id, site, f"{site}/{prefix}/%"))
        await engine._maybe_finish_job(job_id, allow_paused=True)
    engine.db.job_cache.clear()
    engine.db.stats_cache.clear()
    engine.notify()
