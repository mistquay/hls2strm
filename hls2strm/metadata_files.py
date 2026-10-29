"""Durable prepared file writes; retries verify content before finishing a move."""

import hashlib
import os
from pathlib import Path
import secrets
import shutil

from .maintenance_files import entries
from .storage import fingerprint, safe_directory
from .trash import _is_link


def make_directory(path):
    existing = path
    while not os.path.lexists(existing):
        existing = existing.parent
    safe_directory(existing)
    path.mkdir(parents=True, exist_ok=True)
    safe_directory(path)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path, sha):
    if not path.is_file() or _is_link(path.lstat()):
        return False
    return digest(path) == sha


def check_original(path, evidence):
    if (not path.is_file() or _is_link(path.lstat()) or fingerprint(path.stat()) != evidence["fingerprint"]
            or evidence.get("sha256") and digest(path) != evidence["sha256"]):
        raise ValueError(f"预览后文件改变：{path}")


def prepare_files(data, stage, nfo_bytes, images):
    make_directory(stage)
    source, target = Path(data["source"]), Path(data["target"])
    originals, writes, staged = [], {}, []
    if data["mode"] == "takeover":
        for f in data["asset"]["files"]:
            src = source / f["path"]
            check_original(src, f)
            copy = stage / f"copy-{len(originals)}"
            shutil.copyfile(src, copy)
            staged.append(str(copy))
            check_original(src, f)
            sha = digest(copy)
            originals.append({"path": str(src), "sha256": sha, "evidence": f})
            writes[str(target / f["path"])] = {"stage": str(copy), "sha256": sha, "before": None}
    for index, (rel, content) in enumerate([(data["nfo_path"], nfo_bytes), *images.items()]):
        if content is None:
            continue
        dst = target / rel
        copy = stage / f"new-{index}"
        copy.write_bytes(content)
        staged.append(str(copy))
        old = next((f for f in data["asset"]["files"] if f["path"] == rel), None)
        if old:
            check_original(source / rel, old)
            shutil.copyfile(source / rel, stage / "original.nfo")
        writes[str(dst)] = {"stage": str(copy), "sha256": digest(copy),
                            "before": old if data["mode"] != "takeover" else None}
    for destination, write in writes.items():
        temporary = Path(destination).with_name(".hls-metadata-" + secrets.token_hex(16) + ".tmp")
        if os.path.lexists(temporary):
            raise ValueError("准备文件名冲突，请重试")
        write["temporary"] = str(temporary)
    return {"writes": writes, "originals": originals, "staged": staged}


def release_prepared(data, stage):
    safe_directory(stage)
    for name in data["prepared"]["staged"]:
        path = Path(name)
        if path.parent != stage or (os.path.lexists(path) and _is_link(path.lstat())):
            raise ValueError("准备文件路径改变，保留现场")
        path.unlink(missing_ok=True)


def validate_progress(data):
    source, target = Path(data["source"]), Path(data["target"])
    manifest = data["prepared"]
    temporaries = {w["temporary"] for w in manifest["writes"].values()}
    before = {f["path"]: f for f in data["asset"]["files"]}
    for rel, identity in data["asset"]["folders"]:
        if safe_directory(source / rel) != identity:
            raise ValueError("源目录身份改变，保留现场")
    actual, _ = entries(source)
    remaining = set(before)
    for f in actual:
        path = source / f["path"]
        if str(path) in temporaries and f["kind"] != "protected":
            continue
        write = manifest["writes"].get(str(path))
        if write and matches(path, write["sha256"]):
            remaining.discard(f["path"])
            continue
        if f["path"] not in before:
            raise ValueError(f"源目录出现新文件：{path}")
        check_original(path, before[f["path"]])
        remaining.discard(f["path"])
    if remaining and data["mode"] != "takeover":
        raise ValueError("源文件消失，请重新预览")
    for rel in remaining:
        write = manifest["writes"].get(str(target / rel))
        if not write or not matches(target / rel, write["sha256"]):
            raise ValueError("移动中断且两端文件不完整，保留现场")
    if data["mode"] == "takeover" and target.exists():
        safe_directory(target)
        current, _ = entries(target)
        for f in current:
            path = target / f["path"]
            if str(path) in temporaries and f["kind"] != "protected":
                continue
            write = manifest["writes"].get(str(path))
            if not write or not matches(path, write["sha256"]):
                raise ValueError(f"接管目标出现冲突：{path}")


def publish(path, prepared):
    make_directory(path.parent)
    temp = Path(prepared["temporary"])
    if temp.parent != path.parent:
        raise ValueError("准备文件路径越界")
    if os.path.lexists(temp):
        if _is_link(temp.lstat()) or not temp.is_file():
            raise ValueError("准备文件被替换，保留现场")
        temp.unlink()
    if matches(path, prepared["sha256"]):
        return
    if prepared["before"]:
        check_original(path, prepared["before"])
    elif os.path.lexists(path):
        raise ValueError(f"目标已有文件，不覆盖：{path}")
    staged = Path(prepared["stage"])
    if not matches(staged, prepared["sha256"]):
        raise ValueError("准备文件损坏，保留现场")
    try:
        with temp.open("xb") as stream, staged.open("rb") as source:
            shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if prepared["before"]:
            check_original(path, prepared["before"])
            os.replace(temp, path)
        else:
            # Windows rename refuses an existing destination; POSIX rename would replace it.
            if os.name == "nt":
                os.rename(temp, path)
            else:
                os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def finish_files(data):
    validate_progress(data)
    writes = data["prepared"]["writes"]
    # Publish media pointers last, so a failed image download/copy cannot expose a half-prepared entry.
    for destination, prepared in sorted(writes.items(), key=lambda item: item[0].lower().endswith(".strm")):
        publish(Path(destination), prepared)
    if data["mode"] == "takeover":
        validate_progress(data)
        for destination, prepared in writes.items():
            if not matches(Path(destination), prepared["sha256"]):
                raise ValueError("接管目标校验失败，保留源文件")
        for original in data["prepared"]["originals"]:
            path = Path(original["path"])
            if os.path.lexists(path):
                check_original(path, original["evidence"])
                if not matches(path, original["sha256"]):
                    raise ValueError("源文件内容改变，保留两端")
                path.unlink()
