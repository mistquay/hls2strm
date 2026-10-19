"""Shared directory boundaries for libraries and global settings."""

from pathlib import Path


def overlaps(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def library_root(output_dir: Path, directory: str) -> Path:
    path = Path(directory)
    return (path if path.is_absolute() else output_dir / path).resolve()


def check_archive_dir(root: Path, output_dir: Path, libraries) -> None:
    root = root.resolve()
    directories = [("输出根目录", output_dir.resolve())]
    for lib in libraries:
        directories.extend((f"输出库「{lib['name']}」", library_root(output_dir, lib[key]))
                           for key in ("dir", "external_dir") if lib.get(key))
    for name, directory in directories:
        if overlaps(root, directory):
            raise ValueError(f"归档目录 {root} 和{name}的目录 {directory} 重叠，不能互相嵌套")


def check_library_paths(libraries, output_dir: Path, archive_dir: Path) -> None:
    libraries = list(libraries)
    output_dir = output_dir.resolve()
    check_archive_dir(archive_dir, output_dir, libraries)
    checked = []
    for lib in libraries:
        for key in ("dir", "external_dir"):
            directory = lib.get(key, "")
            if directory and not Path(directory).is_absolute() and ".." in Path(directory).parts:
                raise ValueError("相对目录不能包含 ..")
        root = library_root(output_dir, lib["dir"])
        if root == output_dir:
            raise ValueError("库目录不能是输出根目录")
        mine = [root]
        if lib.get("external_dir"):
            external = library_root(output_dir, lib["external_dir"])
            if overlaps(root, external):
                raise ValueError("外部整理目录和库目录不能互相嵌套，否则整理好的文件会被外部工具当成新文件再处理一遍")
            mine.append(external)
        for name, directories in checked:
            for directory in directories:
                if any(overlaps(path, directory) for path in mine):
                    raise ValueError(f"目录和输出库「{name}」（{directory}）重叠，各库的目录、外部整理目录不能互相嵌套")
        checked.append((lib["name"], mine))
