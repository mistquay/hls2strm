"""目录占用扫描与经过确认的 extrafanart 图片清理。清单保存在数据目录，任务可暂停恢复。"""

import asyncio
from collections import Counter
import itertools
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .trash import TRASH_DIR, _is_link

log = logging.getLogger(__name__)
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}


def identity(st):
    return [st.st_dev, st.st_ino]


def fingerprint(st):
    return [*identity(st), st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def safe_directory(path):
    """包括根的祖先也不能通过软链接或 Windows junction 跳转。"""
    for part in [path, *path.parents]:
        st = part.lstat()
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"目录不可用或包含链接：{part}")
    return identity(path.stat())


def natural_key(path):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path.name.casefold())]


def scan_directories(root, keep):
    """逐目录生成统计和固定清单；不跟随链接，不进入回收区。"""
    def fail(error):
        raise error

    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        directory = Path(base)
        safe_directory(directory)
        skipped = 0
        for name in dirs[:]:
            if name == TRASH_DIR or _is_link((directory / name).lstat()):
                dirs.remove(name)
                skipped += 1
        rel = directory.relative_to(root)
        counts = Counter(directories=1, skipped=skipped)
        images = []
        in_extra = root.name.casefold() == "extrafanart" or any(p.casefold() == "extrafanart" for p in rel.parts)
        for name in files:
            path = directory / name
            st = path.lstat()
            if _is_link(st) or not stat.S_ISREG(st.st_mode):
                counts["skipped"] += 1
                continue
            counts["files"] += 1
            counts["bytes"] += st.st_size
            kind = ("extra" if in_extra else "strm" if path.suffix.lower() == ".strm"
                    else "nfo" if path.suffix.lower() == ".nfo"
                    else "images" if path.suffix.lower() in IMAGES else "other")
            counts[kind + "_bytes"] += st.st_size
            if directory.name.casefold() == "extrafanart" and path.suffix.lower() in IMAGES:
                images.append((path, st))
        images.sort(key=lambda item: (natural_key(item[0]), item[0].name))
        candidates = images[keep:]
        counts["delete_files"] = len(candidates)
        counts["delete_bytes"] = sum(st.st_size for _, st in candidates)
        parents = []
        parent = directory
        while parent != root:
            parents.append([str(parent.relative_to(root)), identity(parent.stat())])
            parent = parent.parent
        yield {"path": str(rel), "counts": dict(counts), "parents": parents,
               "files": [[str(p.relative_to(root)), fingerprint(st)] for p, st in candidates]}


def delete_batch(root, root_identity, rows):
    """仅删除清单内未改变的普通图片；不递归删除目录。"""
    if safe_directory(root) != root_identity:
        raise ValueError("扫描根目录已改变，请重新扫描")
    counts = Counter()
    for row in rows:
        try:
            for relative, expected in row["parents"]:
                path = root / relative
                if not path.is_relative_to(root) or safe_directory(path) != expected:
                    raise ValueError("目录已改变")
        except (OSError, ValueError) as error:
            counts["skipped"] += len(row["files"])
            log.warning("跳过变化的目录 %s：%s", row["path"], error)
            continue
        for relative, expected in row["files"]:
            path = root / relative
            try:
                if (not path.is_relative_to(root) or ".." in Path(relative).parts
                        or path.parent.name.casefold() != "extrafanart" or path.suffix.lower() not in IMAGES):
                    raise ValueError("不属于额外图片清理范围")
                st = path.lstat()
                if _is_link(st) or not stat.S_ISREG(st.st_mode) or fingerprint(st) != expected:
                    counts["skipped"] += 1
                    log.warning("跳过变化的图片 %s", path)
                    continue
                path.unlink()
                counts["deleted"] += 1
                counts["deleted_bytes"] += st.st_size
            except FileNotFoundError:
                counts["skipped"] += 1
            except (OSError, ValueError) as error:
                counts["failed"] += 1
                log.warning("清理图片失败 %s：%s", path, error)
    return counts


def read_batch(iterator):
    return list(itertools.islice(iterator, 100))


async def next_batch(iterator):
    worker = asyncio.create_task(asyncio.to_thread(read_batch, iterator))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker  # 文件句柄/生成器关闭前，等正在进行的目录读取结束。
        raise


class Storage:
    def __init__(self, engine, data_dir):
        self.e, self.db = engine, engine.db
        self.directory = data_dir / "storage-plans"

    def root(self, library_id):
        lib = self.e._library(library_id)
        if lib.get("operation_pending"):
            raise ValueError("请先完成输出库配置变更")
        return (self.e.writer.external_root(lib) or self.e.writer.library_root(lib)).absolute()

    def manifest(self, job_id):
        return self.directory / f"{int(job_id)}.jsonl"

    async def create(self, library_id, keep):
        root = self.root(library_id)
        await asyncio.to_thread(safe_directory, root)
        lib = self.e._library(library_id)
        job_id = await self.db.create_job("storage_scan", f"目录统计与清理预览：{lib['name']}",
                                          {"library_id": library_id, "root": str(root), "keep": keep})
        await self.db.add_tasks(job_id, "storage_scan", ["all"], 20)
        self.e.notify()
        return job_id

    async def scan(self, job, task):
        root = self.root(job["params"]["library_id"])
        if str(root) != job["params"]["root"]:
            raise ValueError("库目录已改变，请重新扫描")
        root_id = await asyncio.to_thread(safe_directory, root)
        self.directory.mkdir(parents=True, exist_ok=True)
        counts, groups, largest = Counter(), {}, []
        iterator = scan_directories(root, job["params"]["keep"])
        temporary = self.manifest(job["id"]).with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                while True:
                    await self.e.checkpoint(job["id"])
                    rows = await next_batch(iterator)
                    if not rows:
                        break
                    for row in rows:
                        counts.update(row["counts"])
                        parts = Path(row["path"]).parts
                        group = parts[0] if parts else "（根目录文件）"
                        if parts or row["counts"].get("files"):
                            groups.setdefault(group, Counter()).update(row["counts"])
                        if row["files"]:
                            output.write(json.dumps(row, ensure_ascii=False) + "\n")
                            largest.append({"path": row["path"], "files": len(row["files"]),
                                            "bytes": row["counts"]["delete_bytes"]})
                    largest = sorted(largest, key=lambda r: r["bytes"], reverse=True)[:50]
                    await self.db.update_job(job["id"], state=dict(counts))
            if root_id != await asyncio.to_thread(safe_directory, root):
                raise ValueError("扫描期间根目录已改变，请重新扫描")
            temporary.replace(self.manifest(job["id"]))
        finally:
            iterator.close()
        summary = {"counts": dict(counts), "root": str(root), "keep": job["params"]["keep"],
                   "directories": sorted([{"path": k, **v} for k, v in groups.items()],
                                         key=lambda r: r.get("bytes", 0), reverse=True)[:100],
                   "largest": largest}
        await self.db.update_job(job["id"], state={**dict(counts), "summary": summary,
            "root_identity": root_id, "preview_token": secrets.token_urlsafe(24)})

    async def approve(self, library_id, preview_id, token):
        async with self.db._tx(invalidate=False) as conn:
            preview = await self.db.get_job(preview_id)
            if (not preview or preview["kind"] != "storage_scan" or preview["status"] != "done"
                    or preview["params"]["library_id"] != library_id
                    or not token or not secrets.compare_digest(token, preview["state"].get("preview_token", ""))):
                raise ValueError("预览无效或尚未完成，请重新扫描")
            state = preview["state"]
            if state.get("apply_job_id"):
                return state["apply_job_id"]
            if str(self.root(library_id)) != preview["params"]["root"]:
                raise ValueError("库目录已改变，请重新扫描")
            if not self.manifest(preview_id).is_file():
                raise ValueError("预览清单不存在，请重新扫描")
            cur = await conn.execute("INSERT INTO jobs(kind,name,params,status,created_at) VALUES(?,?,?,'running',?)",
                ("storage_clean", "清理已确认的 extrafanart 图片", json.dumps({"library_id": library_id,
                    "preview_id": preview_id}), int(time.time())))
            job_id = cur.lastrowid
            await conn.execute("INSERT INTO tasks(job_id,kind,target,priority,updated_at) VALUES(?,'storage_clean','all',20,?)",
                               (job_id, int(time.time())))
            await conn.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps({**state, "apply_job_id": job_id}), preview_id))
        self.e.notify()
        return job_id

    async def clean(self, job, task):
        preview = await self.db.get_job(job["params"]["preview_id"])
        root = self.root(job["params"]["library_id"])
        if str(root) != preview["params"]["root"]:
            raise ValueError("库目录已改变，请重新扫描")
        state = Counter(job["state"])
        with self.manifest(preview["id"]).open(encoding="utf-8") as source:
            iterator = itertools.islice(source, state.get("cursor", 0), None)
            while True:
                await self.e.checkpoint(job["id"])
                lines = await next_batch(iterator)
                if not lines:
                    break
                async with self.e.file_changes.exclusive():
                    if root != self.root(job["params"]["library_id"]):
                        raise ValueError("库目录已改变，请重新扫描")
                    # 屏蔽取消直至当前有限批次完成，避免后台线程继续删除而引擎已退出。
                    worker = asyncio.create_task(asyncio.to_thread(delete_batch, root,
                        preview["state"]["root_identity"], [json.loads(line) for line in lines]))
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        result = await worker
                        state.update(result)
                        state["cursor"] += len(lines)
                        await self.db.update_job(job["id"], state=dict(state))
                        raise
                state.update(result)
                state["cursor"] += len(lines)
                state["partial_failure"] = int(bool(state["failed"]))
                await self.db.update_job(job["id"], state=dict(state))
        log.info("额外图片清理完成：删除 %d 个，%d 字节，跳过 %d，失败 %d", state["deleted"],
                 state["deleted_bytes"], state["skipped"], state["failed"])
