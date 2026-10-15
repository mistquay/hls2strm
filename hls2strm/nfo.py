"""有界只读 NFO 解析：禁用 DTD，不访问外部实体或资源 URL。"""

import datetime as dt
from pathlib import Path
import xml.etree.ElementTree as ET

from .storage import fingerprint

MAX_BYTES = 2 * 1024 * 1024
VERSION = 1


class SafeTree(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nodes = 0

    def doctype(self, name, pubid, system):
        raise ValueError("NFO 不允许 DTD 或实体声明")

    def start(self, tag, attrs):
        self.depth += 1
        self.nodes += 1
        if self.nodes > 20000:
            raise ValueError("NFO 节点过多")
        if self.depth > 32:
            raise ValueError("NFO 层级过深")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def parse(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("NFO 超过 2 MiB")
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=SafeTree()))
    except ET.ParseError as error:
        raise ValueError(f"NFO XML 无效：{error}") from None
    if root.tag != "movie":
        raise ValueError("NFO 根节点必须是 movie")

    def text(name):
        return (root.findtext(name) or "").strip()

    def values(name):
        return list(dict.fromkeys((n.text or "").strip() for n in root.findall(name) if (n.text or "").strip()))

    result = {key: text(tag) for key, tag in {
        "title": "title", "originaltitle": "originaltitle", "plot": "plot", "code": "num",
        "maker": "studio", "director": "director", "series": "set/name"}.items()}
    result["series"] = result["series"] or text("set")
    result.update(categories=values("genre"), tags=values("tag"), models=values("actor/name"))
    codes = values("num") + [(n.text or "").strip() for n in root.findall("uniqueid")
                               if n.get("type", "").lower() in ("num", "number", "code") and (n.text or "").strip()]
    result["codes"] = list(dict.fromkeys(codes))
    result["code"] = result["code"] or next(iter(codes), "")
    errors = []
    dates = []
    for name in ("premiered", "releasedate"):
        for raw in values(name):
            try:
                dates.append(dt.date.fromisoformat(raw).isoformat())
            except ValueError:
                errors.append(f"{name} 日期无效：{raw}")
    if len(set(dates)) > 1:
        errors.append("premiered 与 releasedate 日期冲突")
    result["release_date"] = dates[0] if dates and len(set(dates)) == 1 and not errors else ""
    year = text("year")
    result["year"] = int(year) if year.isdigit() and 1000 <= int(year) <= 9999 else None
    if result["release_date"]:
        derived = int(result["release_date"][:4])
        if result["year"] and result["year"] != derived:
            errors.append("发行年份与日期冲突")
        result["year"] = derived
    result["date_precision"] = "day" if result["release_date"] else "year" if result["year"] else "unknown"
    try:
        minutes = float(text("runtime"))
        result["duration"] = round(minutes * 60) if 0 < minutes <= 100000 else None
    except (ValueError, OverflowError):
        result["duration"] = None
    result["ratings"] = [{"value": text("rating"), "source": "rating"}] if text("rating") else []
    result["ratings"] += [{"source": n.get("name", ""), "max": n.get("max", ""),
                            "value": (n.findtext("value") or "").strip()} for n in root.findall("ratings/rating")]
    result["errors"] = errors
    return result


def read(path: Path) -> tuple[dict, list]:
    before = fingerprint(path.stat())
    with path.open("rb") as file:
        data = file.read(MAX_BYTES + 1)
    result = parse(data)
    if fingerprint(path.stat()) != before:
        raise ValueError("读取期间 NFO 发生变化")
    return result, before
