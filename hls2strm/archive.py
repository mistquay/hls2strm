"""下架影片的归档。

影片没有可用的源下架（删源、站点清退）时，它在各输出库里的文件原样搬进归档目录，恢复时搬回原处，
刮削好的 nfo、图片不用重新刮：
- 外部整理目录里只属于这部片的影片目录（strm、nfo、图片、花絮）整个搬走，媒体库就看不到了；
- 收件目录里还没整理的 strm 搬走，外部刮削器就监控不到了；
- 和别的片共用的目录只搬这部片的 strm（和多画质版本），自管库再带上本程序写的 nfo、封面。

归档目录在输出根目录、各库目录和外部整理目录之外，不自动清理；里面按 <库名>/library|external/<原相对路径> 存放，
手工也能搬回去。跨文件系统时复制再删。都是同步函数，调用方放到线程里跑。
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

from .trash import exclusive, inside, prune
from .writer import OUTPUT_SUFFIXES, OutputWriter


def plan(strms: list[Path], slug: str, root: Path, ext: Path | None, dest: Path) -> list[tuple[str, str]]:
    """这部片在一个库里要搬的东西 [(原路径, 归档路径)]。strms 是它在这个库里的 strm（可能不止一个），
    root 是库目录（收件目录），ext 是外部整理目录，dest 是这个库在归档目录里的子目录。"""
    by_dir: dict[Path, list[Path]] = {}
    for p in strms:
        by_dir.setdefault(p.parent, []).append(p)
    sources: list[Path] = []
    for d, own in by_dir.items():
        files = list(own)
        for p in own:
            files += OutputWriter.own_versions(p, slug)
        if ext is not None and inside(d, ext) and exclusive(d, frozenset(os.path.normcase(str(f)) for f in files)):
            sources.append(d)
            continue
        if ext is None:
            for p in own:
                base = p.name.removesuffix(".strm")
                files += [f for s in OUTPUT_SUFFIXES[1:] if (f := p.with_name(base + s)).exists()]
        sources += list(dict.fromkeys(files))
    pairs, taken = [], set()
    for src in sources:
        base, area = (ext, "external") if ext is not None and inside(src, ext) else (root, "library")
        try:
            rel = src.relative_to(base)
        except ValueError:  # 记录的路径不在库目录里（手工挪过）
            rel = Path(src.name)
        target = dest / area / rel
        n = 1
        while os.path.lexists(target) or target in taken:  # 以前归档过同名的
            n += 1
            target = target.with_name(f"{rel.stem} ({n}){rel.suffix}" if src.is_file() else f"{rel.name} ({n})")
        taken.add(target)
        pairs.append((str(src), str(target)))
    return pairs


def move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(src, dst)  # 不在同一个文件系统（Docker 里不同的挂载也算）：复制再删


def stash(pairs: list[tuple[str, str]]) -> int:
    """按 plan 搬进归档目录，返回搬了几个。原路径已经不在的跳过（上次搬到一半）。"""
    n = 0
    for src, dst in pairs:
        if os.path.lexists(src):
            move(Path(src), Path(dst))
            n += 1
    return n


def unstash(pairs: list[tuple[str, str]], root: Path) -> list[str]:
    """搬回原处，再删掉归档目录里因此空出来的目录。原处已经有东西、归档里的不在了的不动，返回这些问题。"""
    problems = []
    for src, dst in pairs:
        if not os.path.lexists(dst):
            problems.append(f"归档里已经没有 {dst}")
        elif os.path.lexists(src):
            problems.append(f"原处已经有 {src}，归档的留在 {dst}")
        else:
            move(Path(dst), Path(src))
            prune(Path(dst).parent, root)
    return problems
