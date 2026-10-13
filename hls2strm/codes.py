"""番号规范化与跨站匹配键。

各站对同一部片的写法不同：Jable 写 `fc2ppv-1066192`，MissAV 写 `fc2-ppv-1066192`；有的站编号补零到 5 位（`ssis00001`）。
匹配键把这些统一：前缀大写、去掉分隔符，编号去掉前导零，FC2 统一成 FC2PPV。
日期型番号（无码厂牌的 `010120-001`）前面没有字母，原样比较；`_` 和 `-` 不能混为一谈：
Caribbeancom 写 `100826-001`，一本道、Caribbeancompr、pacopacomama、10musume 写 `100826_001`，是不同的片。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

_FC2_RE = re.compile(r"^FC2[\s_-]*(?:PPV[\s_-]*)?(\d{5,9})$")
_SPLIT_RE = re.compile(r"[\s_-]+")
_GLUED_RE = re.compile(r"([A-Z]+)(\d+)")
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9._-]+")
_DATE_SEP_RE = re.compile(r"[\s-]+")


def legacy_code_key(code: str) -> str:
    """'SSIS-001' / 'ssis00001' -> 'SSIS-1'；'FC2-491887' / 'fc2ppv-491887' -> 'FC2PPV-491887'。"""
    s = code.strip().upper()
    if not s:
        return ""
    if m := _FC2_RE.match(s):
        return f"FC2PPV-{int(m.group(1))}"
    if not any(c.isalpha() for c in s):
        return _DATE_SEP_RE.sub("-", s)  # 日期型番号：只统一空白和横线，下划线原样保留
    tokens = [t for t in _SPLIT_RE.split(s) if t]
    if len(tokens) == 1:
        m = _GLUED_RE.fullmatch(tokens[0])
        if not m:
            return tokens[0]
        tokens = [m.group(1), m.group(2)]
    # 第一个纯数字、且前面有字母的段是编号，后面的段（分段 -2 等）原样保留
    for i in range(1, len(tokens)):
        prefix = "".join(tokens[:i])
        if tokens[i].isdigit() and any(c.isalpha() for c in prefix):
            return "-".join([prefix, str(int(tokens[i])), *tokens[i + 1:]])
    return "-".join(tokens)


def work_slug(code: str, uncensored: bool = False) -> str:
    """新作品的 slug（/play/{slug}.m3u8 和默认文件名用）：番号小写，无码流出版加 -u。"""
    base = _SLUG_UNSAFE_RE.sub("-", code.strip().lower()).strip("-._") or "x"
    return (base[:78] + "-u") if uncensored else base[:80]


# 原生无码厂牌的日期和序号不能去掉前导零。
STUDIOS = {
    "CARIB": (("CARIB", "CARIBBEANCOM"), "-"),
    "CARIBPR": (("CARIBPR", "CARIBBEANCOMPR"), "_"),
    "1PON": (("1PON", "1PONDO", "PONDO"), "_"),
    "10MU": (("10MU", "10MUSUME", "MUSUME"), "_"),
    "PACO": (("PACO", "PACOPACOMAMA"), "_"),
}
MAKER_STUDIOS = {
    **{alias: name for name, (aliases, _) in STUDIOS.items() for alias in aliases},
    "一本道": "1PON", "カリビアンコム": "CARIB", "カリビアンコムプレミアム": "CARIBPR",
    "パコパコママ": "PACO", "天然むすめ": "10MU",
}
KEEP_PREFIX: set[str] = set()  # 需要保留三位数字前缀的厂牌，按完整首段填写。
_STUDIO_RE = re.compile(
    r"^(" + "|".join(re.escape(alias) for aliases, _ in STUDIOS.values() for alias in aliases)
    + r")[\s_-]*(\d{6})[\s_-]+(\d{2,3})$")
_STUDIO_NAMES = {alias: name for name, (aliases, _) in STUDIOS.items() for alias in aliases}
_HEYZO_RE = re.compile(r"^HEYZO[\s_-]*(?:HD[\s_-]*)?(\d{3,4})$")
_TOKYO_RE = re.compile(r"^(?:TOKYO[\s_-]*HOT[\s_-]*)?([NK])[\s_-]?(\d{4})$")
_NUMBER_PREFIX_RE = re.compile(r"^(\d{3})([A-Z]{2,})$")


def code_key(code: str, maker: str = "") -> str:
    """匹配键 v2：认数字厂牌前缀、原生无码厂牌，日期和序号保留前导零。"""
    s = unicodedata.normalize("NFKC", code).strip().upper()
    if not s:
        return ""
    if m := _FC2_RE.match(s):
        return f"FC2PPV-{int(m.group(1))}"
    if m := _STUDIO_RE.fullmatch(s):
        name = _STUDIO_NAMES[m.group(1)]
        return f"{name}-{m.group(2)}{STUDIOS[name][1]}{m.group(3)}"
    if m := _HEYZO_RE.fullmatch(s):
        return f"HEYZO-{int(m.group(1))}"
    if m := _TOKYO_RE.fullmatch(s):
        return f"TOKYOHOT-{m.group(1)}{m.group(2)}"
    if not any(c.isalpha() for c in s):
        if re.fullmatch(r"\d{6}[\s-]\d{3}", s):
            return "CARIB-" + _DATE_SEP_RE.sub("-", s)
        if re.fullmatch(r"\d{6}_\d{2}", s):
            return "10MU-" + s
        if re.fullmatch(r"\d{6}_\d{3}", s):
            maker = "".join(unicodedata.normalize("NFKC", maker).upper().split())
            # 长名优先，避免 CaribbeancomPR 被 Caribbeancom 的子串抢先匹配。
            for alias in sorted(MAKER_STUDIOS, key=len, reverse=True):
                if STUDIOS[MAKER_STUDIOS[alias]][1] == "_" and alias.upper() in maker:
                    name = MAKER_STUDIOS[alias]
                    return f"{name}-{s[:6]}{STUDIOS[name][1]}{s[7:]}"
        return legacy_code_key(s)
    first = _SPLIT_RE.split(s, maxsplit=1)[0]
    if _NUMBER_PREFIX_RE.fullmatch(first) and first not in KEEP_PREFIX:
        s = s[3:]
    return legacy_code_key(s)


code_key_v2 = code_key  # 保留 P0 审计入口，审计与业务共用同一算法。


def compatible_keys(key: str) -> list[str]:
    """没写厂牌的日期番号（下划线、三位序号）能对上的带厂牌的键，只认下划线厂牌；其他键返回空。

    只有这一个方向：没写厂牌的作品可能是任何一个下划线厂牌的片，带厂牌的源要是能挂到它上面，
    同日期同序号的一本道、パコパコママ等不同的片就会被并成一部。
    """
    if re.fullmatch(r"\d{6}_\d{3}", key):
        return [name + "-" + key for name, (_, separator) in STUDIOS.items() if separator == "_"]
    return []


def compatible(found: str, wanted: str) -> bool:
    """站点上找到的键 found 算不算要找的作品 wanted：相同，或者 found 没写厂牌而 wanted 是对得上的带厂牌写法。"""
    return found == wanted or wanted in compatible_keys(found)


def is_native(key: str) -> bool:
    """原生无码厂牌和裸日期型番号不再细分普通版、流出版。"""
    return key.startswith(tuple(name + "-" for name in (*STUDIOS, "HEYZO", "TOKYOHOT"))) or not any(
        c.isalpha() for c in key)


def edition(code: str, uncensored: bool, maker: str = "") -> bool:
    return bool(uncensored and not is_native(code_key(code, maker)))


def numbered_prefix(code: str) -> tuple[str, str] | None:
    first = _SPLIT_RE.split(unicodedata.normalize("NFKC", code).strip().upper(), maxsplit=1)[0]
    if first not in KEEP_PREFIX and (m := _NUMBER_PREFIX_RE.fullmatch(first)):
        return m.group(2), m.group(1)
    return None


def series_prefix(key: str) -> str:
    """从规范化匹配键提取系列；日期型厂牌和不完整番号不猜前缀。"""
    if key.startswith("TOKYOHOT-") and re.fullmatch(r"TOKYOHOT-[NK]\d{4}", key):
        return "TOKYOHOT"
    if key.partition("-")[0] in STUDIOS:
        return ""
    match = re.fullmatch(r"([A-Z0-9]*[A-Z][A-Z0-9]*)-\d+(?:-[A-Z0-9]+)*", key)
    return match.group(1) if match else ""


class Prefixes(dict):
    """从不同的作品番号学数字前缀；重复抓取同一番号不重复计票。"""
    def __init__(self):
        super().__init__()
        self.seen = set()
        self.counts = defaultdict(Counter)

    def add(self, code: str) -> None:
        code = code.upper()
        pair = numbered_prefix(code)
        if code in self.seen or pair is None:
            return
        self.seen.add(code)
        maker, prefix = pair
        self.counts[maker][prefix] += 1
        self[maker] = min(self.counts[maker], key=lambda p: (-self.counts[maker][p], p))


def spellings(code: str, prefixes: dict[str, str]) -> list[str]:
    """原写法优先，编号保留补零；地址候选由各站去重和限次。"""
    out = [code]
    s = unicodedata.normalize("NFKC", code).strip().upper()
    if numbered_prefix(s):
        out.append(s[3:])
    else:
        maker = code_key(s).split("-", 1)[0]
        if maker in prefixes:
            out.append(prefixes[maker] + s)
    key = code_key(s)
    name, _, number = key.partition("-")
    if name in STUDIOS:
        out.extend(alias + "-" + number for alias in STUDIOS[name][0])
        out.append(number)
    return list(dict.fromkeys(out))
