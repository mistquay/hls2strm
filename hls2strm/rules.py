"""规则库：按分类、标签、女优、画质、关键词、版本自动归库。

规则格式：{"categories": [], "tags": [], "models": [], "quality": [], "keywords": [], "edition": "", "match": "any|all"}
分类、标签可以写站点上的名称（如「中文字幕」）或 slug（如 chinese-subtitle）；女优写名字或 id。
edition：u = -U 版（无码流出 / 破解），normal = 普通版，空 = 不限；原生无码厂牌的番号不分版本，算普通版。
各组条件之间按 match 组合：any = 满足任一组，all = 每组都满足；组内是「任一」。
"""

from __future__ import annotations

from .codes import edition

RULE_LISTS = ("categories", "tags", "models", "quality", "keywords")
EDITIONS = {"u": "-U 版", "normal": "普通版"}


def normalize_rule(raw: dict | None) -> dict | None:
    """清洗规则；没有任何条件时返回 None（不是规则库）。"""
    if not raw:
        return None
    rule: dict = {}
    for key in RULE_LISTS:
        values = raw.get(key) or []
        if isinstance(values, str):
            values = values.replace("，", ",").split(",")
        rule[key] = list(dict.fromkeys(v.strip() for v in values if str(v).strip()))
    rule["edition"] = raw.get("edition") or ""
    if rule["edition"] and rule["edition"] not in EDITIONS:
        raise ValueError("edition 只能是 u（-U 版）、normal（普通版）或留空")
    match = raw.get("match", "any")
    if match not in ("any", "all"):
        raise ValueError("match 只能是 any 或 all")
    rule["match"] = match
    if not any(rule[k] for k in RULE_LISTS) and not rule["edition"]:
        return None
    return rule


def _keys(items: list[dict], *fields: str) -> set[str]:
    return {str(item.get(f, "")).strip().lower() for item in items for f in fields if item.get(f)}


def match_rule(rule: dict, v: dict) -> bool:
    checks = []
    if rule["categories"]:
        have = _keys(v.get("categories") or [], "slug", "name")
        checks.append(any(c.lower() in have for c in rule["categories"]))
    if rule["tags"]:
        have = _keys(v.get("tags") or [], "slug", "name")
        checks.append(any(t.lower() in have for t in rule["tags"]))
    if rule["models"]:
        have = _keys(v.get("models") or [], "id", "name")
        checks.append(any(m.lower() in have for m in rule["models"]))
    if rule["quality"]:
        quality = (v.get("quality") or "").lower()
        checks.append(any(q.lower() in quality for q in rule["quality"]))
    if rule["keywords"]:
        text = f"{v.get('code', '')} {v.get('title', '')}".lower()
        checks.append(any(k.lower() in text for k in rule["keywords"]))
    if rule.get("edition"):
        u = edition(v.get("code") or v.get("slug", ""), bool(v.get("uncensored")), v.get("maker") or "")
        checks.append(u == (rule["edition"] == "u"))
    if not checks:
        return False
    return any(checks) if rule["match"] == "any" else all(checks)


def describe_rule(rule: dict | None) -> str:
    if not rule:
        return ""
    names = {"categories": "分类", "tags": "标签", "models": "女优", "quality": "画质", "keywords": "关键词"}
    parts = [f"{names[k]}：{'、'.join(rule[k])}" for k in RULE_LISTS if rule[k]]
    if rule.get("edition"):
        parts.append(f"版本：{EDITIONS[rule['edition']]}")
    return (" 或 " if rule["match"] == "any" else " 且 ").join(parts)
