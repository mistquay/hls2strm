"""同片整理的本地预览：身份、保留项、文件影响和预计归库，不访问源站。"""

import asyncio
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from .code_audit import conflicts
from .codes import edition
from .operation_plans import context, dependency_order, digest
from .rules import match_rule
from .trash import exclusive, inside


def options(payload):
    if set(payload) - {"keep", "skip", "allow_conflicts"}:
        raise ValueError("未知的同片整理选项")
    keep, skip, allow = payload.get("keep", {}), payload.get("skip", []), payload.get("allow_conflicts", [])
    if not isinstance(keep, dict) or any(not str(k).isdigit() or type(v) is not int or v < 1 for k, v in keep.items()):
        raise ValueError("保留项必须是组编号到影片 ID 的映射")
    if any(not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids) for ids in (skip, allow)):
        raise ValueError("跳过组和冲突确认必须是组编号列表")
    return keep, set(skip), set(allow)


def video_state(video):
    return {key: video[key] for key in ("id", "code_key", "uncensored", "status")}


def metadata_empty(key, value):
    return value in (None, "", []) or (key in ("duration", "views", "likes", "favs", "detail_at") and value == 0)


def output_state(outputs):
    return [{k: o[k] for k in ("library_id", "strm_path", "via")} for o in outputs]


def predict(libs, members, keeper):
    """先合并归属，再按现有规则与来源/排除顺序估计，不写任何输出。"""
    v = dict(keeper)
    for member in members:
        for key, value in member.items():
            if metadata_empty(key, v.get(key)) and not metadata_empty(key, value):
                v[key] = value
    v["uncensored"] = edition(v["code"], v["uncensored"], v["maker"])
    memberships = {o["library_id"]: dict(o) for m in [*members, keeper] for o in m["outputs"]}
    for lib in libs.values():
        if not lib["rule"]:
            continue
        lid = lib["id"]
        should = v["status"] == "active" and match_rule(lib["rule"], v) and not set(memberships).intersection(lib["excludes"])
        if should and lid not in memberships:
            memberships[lid] = {"via": "rule", "strm_path": ""}
        elif not should and memberships.get(lid, {}).get("via") == "rule":
            memberships.pop(lid)
    for lid in dependency_order(libs):
        lib = libs[lid]
        source = any(memberships.get(i, {}).get("strm_path") for i in lib["sources"])
        blocked = bool(set(memberships).intersection(lib["excludes"]))
        if blocked or memberships.get(lid, {}).get("via") == "source" and not source:
            memberships.pop(lid, None)
        if not blocked and source and v["status"] == "active":
            memberships.setdefault(lid, {"via": "source", "strm_path": ""})
    return sorted(memberships)


def file_effects(engine, members, keep):
    files, roots, missing = set(), {}, []
    for member in members:
        if member["id"] == keep:
            continue
        for out in member["outputs"]:
            lib = engine.libs[out["library_id"]]
            ext = engine.writer.external_root(lib)
            paths = out["paths"]
            if out["strm_path"] and not paths:
                missing.append({"slug": member["slug"], "library_id": lib["id"], "path": out["strm_path"]})
            for path in map(Path, paths):
                files.add(str(path))
                files.update(str(p) for p in engine.writer.own_versions(path, member["slug"]))
                if ext and inside(path.parent, ext):
                    roots[path.parent] = ext
    owned = frozenset(os.path.normcase(p) for p in files)
    retired, shared = [], []
    for folder, ext in roots.items():
        (retired if exclusive(folder, owned) else shared).append(str(folder))
    return {"files": sorted(files), "retire_dirs": sorted(retired), "shared_dirs": sorted(shared), "missing": missing}


class DedupePlanner:
    def __init__(self, engine, request):
        self.e, self.db, self.request = engine, engine.db, request
        self.keep, self.skip, self.allow = options(request["payload"])
        self.indexes = {}
        self.plays = {}

    async def groups(self):
        grouped = defaultdict(list)
        async for video in self.db.iter_videos():
            if video["code_key"]:
                grouped[video["code_key"], edition(video["code"], video["uncensored"], video["maker"])].append(video)
        rows = await self.db._all("SELECT video_id,COUNT(*) AS n FROM play_history GROUP BY video_id", bulk=True)
        self.plays = {r["video_id"]: r["n"] for r in rows}
        groups = [members for (_, u), members in grouped.items()
                  if len(members) > 1 or (not u and members[0]["uncensored"])]
        return sorted(groups, key=lambda members: min(v["id"] for v in members))

    async def item(self, members):
        for v in members:
            v["outputs"] = await self.db.get_outputs(v["id"])
            v["sources"] = [{k: s[k] for k in ("id", "site", "key", "status")}
                            for s in await self.db.get_sources(v["id"])]
            v["plays"] = self.plays.get(v["id"], 0)
            for out in v["outputs"]:
                lib = self.e.libs[out["library_id"]]
                if lib.get("operation_pending"):
                    raise ValueError(f"输出库「{lib['name']}」有待执行变更，完成后再整理")
                ext = self.e.writer.external_root(lib)
                if ext:
                    if lib["id"] not in self.indexes:
                        self.indexes[lib["id"]] = await self.e.strm.index(lib, max_age=0)
                    paths = list(self.indexes[lib["id"]].get(v["id"], []))
                else:
                    paths = []
                if out["strm_path"] and await asyncio.to_thread(os.path.isfile, out["strm_path"]):
                    paths = list(dict.fromkeys([*paths, out["strm_path"]]))
                out["paths"] = paths
                out["external_paths"] = [p for p in paths if ext and inside(Path(p), ext)]
        group_id = min(v["id"] for v in members)
        keeper = max(members, key=lambda v: (v["status"] == "active", any(o["external_paths"] for o in v["outputs"]),
                                             v["plays"], sum(bool(o["paths"]) for o in v["outputs"]), -v["id"]))
        if str(group_id) in self.keep:
            keeper = next((v for v in members if v["id"] == self.keep[str(group_id)]), None)
            if keeper is None:
                raise ValueError(f"组 #{group_id} 的保留项不在组内")
        clash = conflicts(members)
        selected = group_id not in self.skip and (not clash or group_id in self.allow)
        predicted = predict(self.e.libs, members, keeper)
        effects = await asyncio.to_thread(file_effects, self.e, members, keeper["id"])
        snapshot = [{"video": video_state(v), "outputs": output_state(v["outputs"]), "sources": v["sources"]}
                    for v in members]
        return {"group_id": group_id, "code_key": keeper["code_key"], "uncensored": edition(keeper["code"], keeper["uncensored"], keeper["maker"]),
                "keep": keeper["id"], "members": members, "conflicts": clash, "selected": selected,
                "predicted_libraries": predicted, "changes_library": predicted != sorted(o["library_id"] for o in keeper["outputs"]),
                "corrections": sum(bool(v["uncensored"]) and not edition(v["code"], v["uncensored"], v["maker"]) for v in members),
                "input": snapshot, **effects}


async def calculate(engine, request, job_id, save=False):
    from .operations import ScopeChanged
    baseline = context(engine, request)
    planner = DedupePlanner(engine, request)
    groups = await planner.groups()
    ids = {min(v["id"] for v in members) for members in groups}
    if (set(map(int, planner.keep)) | planner.skip | planner.allow) - ids:
        raise ScopeChanged("选择的重复组已改变，请重新预览")
    counts = dict.fromkeys(("groups", "selected_groups", "merged_videos", "strm_files", "retire_dirs", "missing_outputs",
                            "library_changes", "conflict_groups", "edition_corrections"), 0)
    fingerprint = hashlib.sha256(digest([baseline, request]).encode())
    samples, attention = [], []
    if save:
        await engine.db._write("DELETE FROM operation_items WHERE preview_id=?", (job_id,))
    for members in groups:
        await engine.checkpoint(job_id)
        item = await planner.item(members)
        counts["groups"] += 1
        counts["conflict_groups"] += bool(item["conflicts"])
        if item["selected"]:
            counts["selected_groups"] += 1
            counts["merged_videos"] += len(members) - 1
            counts["strm_files"] += len(item["files"])
            counts["retire_dirs"] += len(item["retire_dirs"])
            counts["missing_outputs"] += len(item["missing"])
            counts["library_changes"] += item["changes_library"]
            counts["edition_corrections"] += item["corrections"]
        fingerprint.update(digest([item["group_id"], item["input"], item["keep"], item["selected"]]).encode())
        if item["conflicts"] or item["changes_library"]:
            attention.append(item)
        elif len(samples) < 20:
            samples.append(item)
        if save:
            await engine.db._write("INSERT INTO operation_items(preview_id,video_id,data) VALUES(?,?,?)",
                                   (job_id, item["group_id"], json.dumps(item, ensure_ascii=False)))
            await engine.db.update_job(job_id, state={"checked": counts["groups"]})
    if digest(baseline) != digest(context(engine, request)):
        raise ScopeChanged("预览期间配置变化，请重新预览")
    counts["matched"] = counts["groups"]
    result = {"counts": counts, "samples": [], "groups": samples, "attention": attention,
              "library_names": {str(k): v["name"] for k, v in engine.libs.items()},
              "warnings": ["同版重复会清理并入者文件；独占的整理目录按回收策略处理，共用目录保留其他文件。",
                           "Emby 下次扫库会移除被清理条目，其观看记录可能丢失。可以更换保留项或跳过该组。",
                           "疑似冲突默认跳过；路径按内容索引定位，执行时重新核对。"]}
    return fingerprint.hexdigest(), result, baseline
