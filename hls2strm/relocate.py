"""已刮削目录搬运：先保存恢复凭据，异常时保留两端，不退回删除。"""

import hashlib
import json
import os
from pathlib import Path

from . import archive
from .trash import _is_link, exclusive, inside
from .writer import OutputWriter, write_atomic


def safe_path(path: Path, root: Path) -> bool:
    if not inside(path, root):
        return False
    # 检查所有祖先，防止整理根目录或中间目录是软链接 / Windows 联接。
    return all(not _is_link(os.lstat(p)) for p in (path, *path.parents) if os.path.lexists(p))


def transfer(strm: Path | None, src: Path, dst: Path, slug: str, journal: Path) -> str:
    """返回新主文件；没有独占目录返回空；冲突和移动异常抛出，禁止调用方删旧写新。"""
    if journal.exists():
        entry = json.loads(journal.read_text(encoding="utf-8"))
        original, target = Path(entry["source"]), Path(entry["target"])
        if not safe_path(original, src) or not safe_path(target, dst):
            raise ValueError("搬运恢复路径不在原整理目录内，保留现场")
        if not original.exists() and target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("已搬运的 strm 内容发生变化，请同步位置后核对")
            return str(target)
        if target.parent.exists():
            raise ValueError("搬运两端同时存在，可能跨盘复制中断；保留现场，请核对后重试")
        strm = original
    if strm is None:
        return ""
    folder = strm.parent
    target = dst / folder.relative_to(src) / strm.name
    if not safe_path(folder, src) or not safe_path(target, dst):
        raise ValueError("搬运路径含软链接或越过整理目录，保留原文件")
    own = frozenset(os.path.normcase(str(p)) for p in (strm, *OutputWriter.own_versions(strm, slug)))
    if not exclusive(folder, own):
        return ""
    if not dst.is_dir():
        raise ValueError("目标整理目录不可访问，请检查挂载后重试")
    if os.path.lexists(target.parent):
        raise ValueError("目标已有同名目录，保留原刮削目录，不覆盖或删除")
    journal.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(journal, json.dumps({"source": str(strm), "target": str(target),
                                     "sha256": hashlib.sha256(strm.read_bytes()).hexdigest()},
                                    ensure_ascii=False).encode("utf-8"))
    archive.move(folder, target.parent)
    return str(target)
