"""维护扫描的文件分类和指纹。所有路径仅来自已配置的库根目录。"""

import os
import hashlib
from pathlib import Path
import stat
import time

from .storage import IMAGES, fingerprint, identity, safe_directory
from .strm_manage import classify, video_of
from .trash import MEDIA_SUFFIXES, TRASH_DIR, _is_link

RESOURCE_TYPES = {"extra", "poster", "fanart", "thumb", "nfo", "trailer", "subtitle"}


def kind(path):
    suffix, stem = path.suffix.lower(), path.stem.lower()
    if suffix == ".strm":
        return "strm"
    if suffix == ".nfo":
        return "nfo"
    if suffix in IMAGES:
        if any(p.casefold() == "extrafanart" for p in path.parts[:-1]):
            return "extra"
        for label in ("poster", "fanart", "thumb"):
            if stem == label or stem.endswith("-" + label):
                return label
        return "other"
    if suffix in (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"):
        return "subtitle"
    if suffix in MEDIA_SUFFIXES:
        return "trailer" if stem == "trailer" or stem.endswith("-trailer") or "trailers" in path.parts else "media"
    return "other"


def fail(error):
    raise error


def area_size(root, visiting=None):
    if not root.exists():
        return 0
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        if visiting:
            visiting(base)
        safe_directory(Path(base))
        dirs[:] = [name for name in dirs if not _is_link((Path(base) / name).lstat())]
        for name in files:
            st = (Path(base) / name).lstat()
            if not _is_link(st) and stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def entries(directory, recursive=True, visiting=None):
    result, folders = [], []
    for base, dirs, files in os.walk(directory, followlinks=False, onerror=fail):
        current = Path(base)
        safe_directory(current)
        folders.append([str(current.relative_to(directory)), identity(current.stat())])
        for name in dirs[:]:
            p = current / name
            if name == TRASH_DIR or _is_link(p.lstat()):
                dirs.remove(name)
                result.append({"path": str(p.relative_to(directory)), "kind": "protected", "size": 0})
        for name in files:
            p = current / name
            if visiting:
                visiting(p)
            st = p.lstat()
            protected = _is_link(st) or not stat.S_ISREG(st.st_mode)
            result.append({"path": str(p.relative_to(directory)), "kind": "protected" if protected else kind(p.relative_to(directory)),
                           "size": 0 if protected else st.st_size, "fingerprint": fingerprint(st), "links": st.st_nlink})
            if not protected and p.suffix.lower() in (".nfo", ".strm") and st.st_size <= 2 * 1024 * 1024:
                result[-1]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        if len(result) > 10000:
            raise ValueError(f"单个影片目录超过 10000 项，需先整理共享目录：{directory}")
        if not recursive:
            break
    return result, folders


def walk(root, keys, cdn, visiting=None):
    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        directory = Path(base)
        if visiting:
            visiting(directory)
        safe_directory(directory)
        for name in dirs[:]:
            if name == TRASH_DIR or _is_link((directory / name).lstat()):
                dirs.remove(name)
        if not files:
            continue
        movie = any(Path(n).suffix.lower() in (".strm", ".nfo") for n in files)
        listing, folders = entries(directory, recursive=movie, visiting=visiting)
        if movie:
            dirs[:] = []
        vids, unknown, strms = set(), [], []
        for f in listing:
            if f["kind"] != "strm":
                continue
            path = directory / f["path"]
            with path.open("rb") as source:
                content = source.read(8192).decode("utf-8", "replace")
            info = classify(path, content, time.time())
            _, vid = video_of(info, cdn, keys)
            if vid is not None and info.kind in ("ours", "version", "cdn"):
                vids.add(vid)
                strms.append({"path": f["path"], "video_id": vid, "version": info.kind == "version"})
            else:
                unknown.append(f["path"])
        shared = len(vids) > 1 or any(Path(f["path"]).parent != Path(".") for f in listing if f["kind"] == "strm")
        status = "conflict" if shared or (vids and unknown) else "present" if len(vids) == 1 else "unmanaged"
        yield {"path": str(directory.relative_to(root)), "files": listing, "folders": folders,
               "video_id": next(iter(vids)) if len(vids) == 1 else None, "status": status, "strms": strms,
               "error": "共享目录或未知 STRM，需确认归属" if status == "conflict" else ""}


def validate(root, data, *, strict=True):
    safe_directory(root)
    directory = root / data["path"]
    if ".." in Path(data["path"]).parts or not directory.is_relative_to(root):
        raise ValueError("目录超出根目录")
    for rel, expected in data["folders"]:
        if safe_directory(directory / rel) != expected:
            raise ValueError("目录身份已改变")
    if strict:
        actual, _ = entries(directory)
        before = {f["path"]: (f.get("fingerprint"), f.get("sha256")) for f in data["files"]}
        after = {f["path"]: (f.get("fingerprint"), f.get("sha256")) for f in actual}
        if before != after:
            raise ValueError("预览后目录文件发生变化，请重新扫描预览")
    return directory
