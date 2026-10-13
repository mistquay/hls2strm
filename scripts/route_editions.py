"""部署后按版本归库；默认只生成预览，--apply 才改配置和执行。

python scripts/route_editions.py --url http://host:18090 --user admin
python scripts/route_editions.py --url http://host:18090 --user admin --apply
密码从 HLS2STRM_ADMIN_PASSWORD 或交互输入读取，不写入脚本。
运行前暂停 MDCNG 对相关目录的监控/整理，并备份数据库和媒体目录。
"""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

LIBRARY_FIELDS = ("name", "dir", "external_dir", "path_template", "versions", "rule", "sources", "excludes")
SUB_FIELDS = ("name", "site", "source", "sort", "library_id", "detail", "interval", "cron", "timezone",
              "stop_after_known", "max_pages", "enabled")


class Client:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, path, body=None, method=None):
        request = Request(self.url + "/api/" + path, data=None if body is None else json.dumps(body).encode(),
                          method=method or ("GET" if body is None else "POST"),
                          headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"{path}: HTTP {error.code} {error.read().decode()}") from None

    def wait(self, job_id):
        while True:
            job = self.call(f"jobs/{job_id}")
            if job["status"] in ("done", "cancelled", "paused"):
                if job["status"] != "done" or job["state"].get("partial_failure"):
                    raise RuntimeError(f"任务 #{job_id} 未完成，请在控制台处理后重跑脚本；已完成部分保留")
                return job
            print(f"任务 #{job_id}：{job['status']}，已处理 {job['state'].get('processed', job['state'].get('checked', 0))}", flush=True)
            time.sleep(5)

    def operation(self, request, directory, label, apply):
        job = self.call("operations/preview", request)["job_id"]
        self.wait(job)
        preview = self.call(f"operations/{job}")
        save(directory / f"{label}-preview-{job}.json", preview)
        print(label, json.dumps(preview["preview"]["counts"], ensure_ascii=False), flush=True)
        if apply:
            applied = self.call(f"operations/{job}/apply", {"preview_token": preview["preview_token"]})["job_id"]
            save(directory / f"{label}-result-{applied}.json", self.wait(applied))
        return preview


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def edition_library(library):
    values = {k: library[k] for k in LIBRARY_FIELDS}
    values["name"] = "无码破解与流出"
    values["rule"] = {"edition": "u", "match": "all"}
    return {"kind": "library", "payload": {"library_id": library["id"], "values": values}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--target-library", type=int, default=7)
    parser.add_argument("--native-library", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".tmp/edition-routing-reports"))
    args = parser.parse_args()
    password = os.environ.get("HLS2STRM_ADMIN_PASSWORD") or getpass.getpass("管理密码：")
    client = Client(args.url, args.user, password)
    directory = args.output / time.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, mode=0o700)
    settings = client.call("settings")
    if "relocate_scraped" not in settings["schema"]:
        raise SystemExit("线上仍是旧版本，请先部署新镜像；未修改任何配置。")
    libraries = client.call("libraries")
    subscriptions = client.call("subscriptions")
    save(directory / "before.json", {"settings": settings["values"], "libraries": libraries, "subscriptions": subscriptions})
    target = next(lib for lib in libraries if lib["id"] == args.target_library)
    native = next(lib for lib in libraries if lib["id"] == args.native_library)
    if not target["external_dir"] or not native["external_dir"] or target["id"] not in native["excludes"]:
        raise SystemExit("库配置与换库条件不符：需外部整理目录，原生库需排除目标库。")
    if target.get("operation_pending") or native.get("operation_pending"):
        raise SystemExit("已有未完成库变更，请先在任务页完成或处理后重试。")
    if args.apply:
        client.call("settings", {"relocate_scraped": True}, "PUT")
        # 先找回 MDCNG 移走的文件；内置搬运保留相对目录、NFO、图片及多画质附件。
        for lib in libraries:
            if lib["external_dir"] and (lib["id"] == target["id"] or target["id"] in lib["excludes"]):
                client.wait(client.call(f"libraries/{lib['id']}/locate", {})["job_id"])
    client.operation(edition_library(target), directory, "edition-routing", args.apply)
    if not args.apply:
        print(f"仅预览，未移动文件。报告：{directory}。确认后使用 --apply。")
        return
    # 归库执行完成后再改订阅，避免旧归属在搬运之前触发清理。
    for sub in subscriptions:
        if sub["site"] in ("missav", "123av") and "uncensored-leak" in sub["source"] and sub["library_id"] == native["id"]:
            values = {k: sub[k] for k in SUB_FIELDS if k in sub}
            values["library_id"] = target["id"]
            client.call(f"subscriptions/{sub['id']}", values, "PUT")
    values = {k: native[k] for k in LIBRARY_FIELDS}
    values["name"] = "原生无码"
    client.operation({"kind": "library", "payload": {"library_id": native["id"], "values": values}}, directory, "native-name", True)
    save(directory / "after.json", {"libraries": client.call("libraries"), "subscriptions": client.call("subscriptions")})
    print(f"完成。报告和原配置：{directory}。恢复 MDCNG 监控，随后让 Emby/FYMS 扫库。")
    print("脚本不修改 MDCNG 历史 target_path，也不迁移 Emby 条目或观看记录。")
    print("未创建存量补源任务。需要时在影片库逐站选择少量影片补源，可在任务页单独暂停。")


if __name__ == "__main__":
    main()
