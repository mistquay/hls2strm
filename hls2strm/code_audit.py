"""升级前的番号只读审计：python -m hls2strm.code_audit --db 副本.db [--out groups.csv]。

仅依赖标准库和番号纯函数，不导入应用、数据库迁移或文件整理模块，不扫描媒体文件。
分组按 v2 键和规整后的版本；时长冲突的 10% 以两部片中较长的时长为基准。
"""

from __future__ import annotations

import argparse
import csv
import json
import ntpath
import posixpath
import sqlite3
from collections import defaultdict
from contextlib import closing
from itertools import combinations
from pathlib import Path, PurePosixPath, PureWindowsPath

from .codes import code_key_v2, edition


def recorded_external(path: str, external_dir: str, output_dir: str = "") -> bool | None:
    """只比较记录的路径；支持在 Windows 上审计 Linux 库副本，根目录不明时返回 None。"""
    if not path or not external_dir:
        return False
    windows = bool(PureWindowsPath(external_dir).drive or "\\" in external_dir
                   or PureWindowsPath(output_dir).drive or "\\" in output_dir)
    pure, paths = (PureWindowsPath, ntpath) if windows else (PurePosixPath, posixpath)
    root = pure(external_dir)
    if not root.is_absolute():
        if not output_dir or not pure(output_dir).is_absolute():
            return None
        root = pure(output_dir) / root
    target, root = pure(paths.normpath(path)), pure(paths.normpath(str(root)))
    if not target.is_absolute():
        return None
    return root in target.parents


def conflicts(members: list[dict]) -> list[dict]:
    """两部都有详情才比较；缺失的日期或时长不作为冲突证据。"""
    found = []
    for a, b in combinations(members, 2):
        if not a["detail_at"] or not b["detail_at"]:
            continue
        reasons = []
        if a["release_date"] and b["release_date"] and a["release_date"] != b["release_date"]:
            reasons.append("发行日期不同")
        da, db = a["duration"], b["duration"]
        if da and db and da > 0 and db > 0 and abs(da - db) > max(120, max(da, db) * 0.1):
            reasons.append("时长差超过 max(120 秒, 较长时长的 10%)")
        if reasons:
            found.append({"video_ids": [a["id"], b["id"]], "reasons": reasons})
    return found


def audit(db_path: Path) -> dict:
    """只读事务内完成统计，v21 可直接审计，不调用 Database.open 或任何迁移。"""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        grouped = defaultdict(list)
        changed = total = 0
        for row in conn.execute("""SELECT id,slug,code,code_key,uncensored,maker,status,
                                          detail_at,release_date,duration FROM videos ORDER BY id"""):
            video = dict(row)
            key = code_key_v2(video["code"], video["maker"])
            total += 1
            changed += key != video["code_key"]
            if key:
                grouped[key, edition(video["code"], video["uncensored"], video["maker"])].append(video)
        siblings = sum(len(members) for (key, u), members in grouped.items() if (key, not u) in grouped)
        groups = []
        videos = {}
        for (key, u), members in sorted(grouped.items()):
            if len(members) < 2:
                continue
            groups.append({"code_key": key, "uncensored": u, "members": members, "conflicts": conflicts(members)})
            for video in members:
                video["outputs"] = []
                videos[video["id"]] = video
        raw = conn.execute("SELECT value FROM settings WHERE key='settings'").fetchone()
        output_dir = json.loads(raw[0]).get("output_dir", "") if raw else ""
        unknown = 0
        if videos:
            for row in conn.execute("""SELECT o.video_id,o.library_id,l.name AS library_name,
                                              o.strm_path,l.external_dir
                                       FROM outputs o LEFT JOIN libraries l ON l.id=o.library_id
                                       ORDER BY o.video_id,o.library_id"""):
                if row["video_id"] not in videos:
                    continue
                out = dict(row)
                out["in_external"] = recorded_external(out["strm_path"], out["external_dir"] or "", output_dir)
                unknown += out["in_external"] is None
                videos[out.pop("video_id")]["outputs"].append(out)
        return {"schema_version": version, "videos": total, "changed_keys": changed,
                "duplicate_groups": groups, "conflict_groups": sum(bool(g["conflicts"]) for g in groups),
                "sibling_videos": siblings, "unknown_external_paths": unknown}


def print_report(report: dict) -> None:
    print(f"只读审计：数据库 v{report['schema_version']}，共 {report['videos']} 部影片")
    print(f"键变化的影片数：{report['changed_keys']}")
    groups = report["duplicate_groups"]
    print(f"同版重复组：{len(groups)}")
    for group in groups:
        print(f"  {group['code_key']} · {'-U 版' if group['uncensored'] else '普通版'}")
        for v in group["members"]:
            print(f"    #{v['id']} {v['slug']} · {v['status']} · 原键 {v['code_key']} · 原版本 {v['uncensored']}")
            if not v["outputs"]:
                print("      未归入输出库")
            for out in v["outputs"]:
                where = {True: "是", False: "否", None: "无法判断"}[out["in_external"]]
                print(f"      库 #{out['library_id']} {out['library_name']} · {out['strm_path'] or '未写出'} · 外部整理目录：{where}")
    print(f"疑似冲突组：{report['conflict_groups']}")
    for group in groups:
        for conflict in group["conflicts"]:
            print(f"  {group['code_key']} · 影片 {conflict['video_ids']}：{'；'.join(conflict['reasons'])}")
    print(f"有兄弟版本的作品数：{report['sibling_videos']}（按影片行计数，包含所有状态）")
    print("路径归属仅按记录判断，未检查文件是否存在或是否已被移动。")
    if report["unknown_external_paths"]:
        print(f"有 {report['unknown_external_paths']} 条路径缺少可确定的输出根目录，外部目录归属无法判断。")


def write_csv(report: dict, path: Path) -> None:
    """每个成员的每条输出一行；没有输出的成员也保留一行。"""
    fields = ("code_key", "edition", "conflicts", "video_id", "slug", "code", "old_code_key", "old_uncensored",
              "status", "maker", "detail_at", "release_date", "duration", "library_id", "library_name",
              "strm_path", "external_dir", "in_external")
    # 不覆盖已有文件，避免 --out 误指向数据库或以前的审计结果。
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for group in report["duplicate_groups"]:
            for v in group["members"]:
                common = {k: v[k] for k in ("slug", "code", "status", "maker", "detail_at", "release_date", "duration")}
                common.update(code_key=group["code_key"], edition="-U" if group["uncensored"] else "普通",
                              conflicts=json.dumps(group["conflicts"], ensure_ascii=False) if group["conflicts"] else "",
                              video_id=v["id"], old_code_key=v["code_key"], old_uncensored=v["uncensored"])
                for out in v["outputs"] or [{}]:
                    row = {**common, **out}
                    row["in_external"] = {True: "是", False: "否", None: "无法判断"}[out.get("in_external", False)]
                    writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="番号别名只读审计，不迁移数据库、不扫描媒体文件")
    parser.add_argument("--db", type=Path, required=True, help="待审计的 SQLite 数据库副本")
    parser.add_argument("--out", type=Path, help="导出重复组 CSV（UTF-8，不覆盖已有文件）")
    args = parser.parse_args(argv)
    try:
        if args.out and args.out.resolve() in {Path(str(args.db.resolve()) + suffix)
                                              for suffix in ("", "-wal", "-shm", "-journal")}:
            raise ValueError("CSV 不能写到数据库或其辅助文件路径")
        report = audit(args.db)
        if args.out:
            write_csv(report, args.out)
        print_report(report)
        if args.out:
            print(f"重复组已导出：{args.out}")
    except (OSError, sqlite3.Error, ValueError) as e:
        parser.exit(1, f"审计失败：{e}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
