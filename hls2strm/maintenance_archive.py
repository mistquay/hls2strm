"""库外归档的文件校验及不覆盖式移动，支持跨挂载复制。"""

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .storage import fingerprint, safe_directory
from .trash import _is_link


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def evidence(path, expected):
    st = path.lstat()
    if _is_link(st) or not stat.S_ISREG(st.st_mode) or fingerprint(st) != expected:
        raise ValueError("预览期间文件已改变")
    result = [*expected, checksum(path)]
    if fingerprint(path.lstat()) != expected:
        raise ValueError("读取文件期间文件已改变")
    return result


def matches(path, expected, *, moved=False):
    if not path.exists():
        return False
    st = path.lstat()
    if _is_link(st) or not stat.S_ISREG(st.st_mode):
        return False
    if (st.st_size != expected[2] if moved else fingerprint(st) != expected[:5]):
        return False
    return checksum(path) == expected[5]


def check_root(root, *, create=False, identity=None):
    """归档目录可尚未创建，但已有祖先不得是链接。"""
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    current = [str(ancestor), safe_directory(ancestor)]
    if identity and safe_directory(Path(identity[0])) != identity[1]:
        raise ValueError("归档根目录或已有祖先身份改变，请检查挂载")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        safe_directory(root)
    return current


def remove_empty_folders(assets):
    for asset in assets:
        if asset["data"].get("partial"):
            continue
        root = Path(asset["root"])
        directory = root / asset["path"]
        folders = [directory / rel for rel, _ in asset["data"]["folders"]]
        for path in sorted(folders, key=lambda p: len(p.parts), reverse=True):
            if path == root or not path.is_relative_to(root):
                continue
            try:
                safe_directory(path)
                path.rmdir()  # 只删冻结清单里的空目录，不递归删除后来出现的文件。
            except (OSError, ValueError):
                pass


def transfer(source, destination, expected):
    from .maintenance_execute import no_replace

    if destination.exists():
        if not matches(destination, expected, moved=True):
            raise ValueError("归档目标冲突或文件改变，保留两端")
        if source.exists():
            if not matches(source, expected, moved=True):
                raise ValueError("原文件已改变，保留两端")
            source.unlink()  # 跨挂载复制完成、源文件删除前进程退出。
        return
    try:
        no_replace(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # 临时文件与最终目标同挂载；最终发布仍不覆盖已有文件。
        fd, temporary = tempfile.mkstemp(prefix=".hls2strm-", dir=destination.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as original:
                shutil.copyfileobj(original, target)
                target.flush()
                os.fsync(target.fileno())
            if not matches(source, expected, moved=True) or not matches(temporary, expected, moved=True):
                raise ValueError("跨挂载复制校验失败，保留原文件")
            no_replace(temporary, destination)
            source.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()


def change_file(row, root, archive_root, *, restore=False, archive_identity=None):
    from .maintenance_execute import checked_parent

    original, destination = Path(row["source"]), Path(row["destination"])
    expected = json.loads(row["fingerprint"])
    checked_parent(original, root, create=restore)
    check_root(archive_root, create=not restore, identity=archive_identity)
    checked_parent(destination, archive_root, create=not restore)
    if restore:
        if original.exists():
            if row["state"] in ("restoring", "restored") and matches(original, expected, moved=True):
                if destination.exists():
                    if not matches(destination, expected, moved=True):
                        raise ValueError("归档文件已变化，保留两端")
                    destination.unlink()
                return "restored"
            raise ValueError("原路径已有文件，未覆盖")
        if not matches(destination, expected, moved=True):
            raise ValueError("归档文件已变化或缺失")
        transfer(destination, original, expected)
        return "restored"
    if destination.exists():
        if original.exists() and not matches(original, expected):
            raise ValueError("原文件已改变，保留两端")
        transfer(original, destination, expected)
        return "trashed"
    if not matches(original, expected):
        raise ValueError("原文件已变化或缺失，未归档")
    transfer(original, destination, expected)
    return "trashed"
