import asyncio

import pytest

from hls2strm.codes import Prefixes, code_key, compatible, spellings
from hls2strm.db import _migrate_v22
from hls2strm.engine import Engine
from hls2strm.errors import NotFound
from hls2strm.sites import SITES, SourceDetail, SourceItem, find_by_code
from hls2strm.sites.base import Site
from hls2strm.sites.supjav import parse_title


def test_attach_alias_editions_and_maker(make_store):
    async def run():
        db, store = await make_store()
        rank = store.current.site_rank
        async def add(key, code, u=False, maker=""):
            slug = Engine._slug_for(SITES["missav"], key, code, u, maker)
            return await db.upsert_detail("missav", SourceDetail(key=key, code=code, title=key,
                                          uncensored=u, maker=maker), slug, rank)
        a = await add("a", "259LUXU-1850", True)
        assert await add("b", "LUXU-1850", True) == a
        assert await add("c", "LUXU-1850") != a
        assert db.prefixes == {"LUXU": "259"}
        n = await add("d", "PONDO-100826_001", True)
        assert await add("e", "1pondo_100826_001") == n
        assert await add("f", "100826_001", True, "一本道") == n
        v = await db.get_video_by_id(n)
        assert not v["uncensored"] and not v["slug"].endswith("-u")
        assert Engine._slug_for(SITES["jable"], "n1234-u", "n1234", True) == "n1234"
        bare = await add("bare", "010120_001")
        await db._update_work(bare, {"maker": "一本道"})
        assert (await db.get_video_by_id(bare))["code_key"] == "1PON-010120_001"
        await db.update_source((await db.find_source("missav", "a"))["id"], status="removed")
        assert await add("a", "259LUXU-1850", True) == a
        assert (await db.find_source("missav", "a"))["status"] == "removed"
    asyncio.run(run())


def test_migrate_v22_changes_only_keys_and_negative_checks(make_store):
    async def run():
        db, _ = await make_store()
        await db._write("""INSERT INTO videos(id,slug,code,code_key,maker,uncensored,created_at,updated_at)
                           VALUES(1,'old-u','259LUXU-1850','259LUXU-1850','',1,1,1),
                                 (2,'native-u','100826_001','100826_001','一本道',1,1,1),
                                 (3,'ssis-1','SSIS-001','SSIS-1','',0,1,1)""")
        for vid in (1, 2, 3):
            await db.set_source_check(vid, "jable", False)
            await db.set_source_check(vid, "missav", True)
        await db.ensure_output(1, 1)
        await db.set_output(1, 1, "/not-mounted/old-u.strm")
        before = [dict(r) for r in await db._all("SELECT * FROM videos ORDER BY id")]
        async with db._tx() as conn:
            await _migrate_v22(conn)
        after = [dict(r) for r in await db._all("SELECT * FROM videos ORDER BY id")]
        assert [v.pop("code_key") for v in after] == ["LUXU-1850", "1PON-100826_001", "SSIS-1"]
        for v in before:
            v.pop("code_key")
        assert before == after
        assert not await db.get_source_check(1, "jable") and not await db.get_source_check(2, "jable")
        assert await db.get_source_check(3, "jable")
        assert all([await db.get_source_check(v, "missav") for v in (1, 2, 3)])
        assert (await db.get_output(1, 1))["strm_path"] == "/not-mounted/old-u.strm"
    asyncio.run(run())


def test_prefix_votes_and_spellings():
    prefixes = Prefixes()
    for code in ("259LUXU-001", "259LUXU-001", "300LUXU-002", "300LUXU-003", "1000GIRI-1"):
        prefixes.add(code)
    assert prefixes == {"LUXU": "300"}
    assert spellings("LUXU-001", prefixes) == ["LUXU-001", "300LUXU-001"]
    assert spellings("259LUXU-001", prefixes) == ["259LUXU-001", "LUXU-001"]
    assert "100826_001" in spellings("1PON-100826_001", {})
    keys = SITES["missav"].key_candidates("1PON-100826_001", "", False, spellings("1PON-100826_001", {}))
    assert keys[:3] == ["1pon-100826_001", "pondo-100826_001", "100826_001"]


def test_lookup_alternates_and_search_edition():
    class Fake(Site):
        def key_for(self, code, subtitle="", uncensored=False):
            return code.lower()

        def detail_path(self, key):
            return "/" + key

        async def fetch_detail(self, sf, key, priority=False):
            calls.append(key)
            if key != "luxu-1850":
                raise NotFound(key)
            return SourceDetail(key=key, code="LUXU-1850", title="", uncensored=True)

        async def lookup(self, sf, code, uncensored=False, priority=False):
            calls.append(code)
            return [SourceItem(key="1", code="259LUXU-1850", title="", uncensored=True)], "fake"
    calls = []
    site = Fake()
    result = asyncio.run(find_by_code(site, None, "259LUXU-1850", True))
    assert len(result) == 1 and calls == ["259luxu-1850", "luxu-1850"]
    calls.clear()
    site.lookup_verified = True
    assert asyncio.run(find_by_code(site, None, "259LUXU-1850", True))
    assert calls == ["LUXU-1850"]
    assert not asyncio.run(find_by_code(site, None, "LUXU-1850", False))
    calls.clear()
    site.lookup_verified = False
    assert not asyncio.run(find_by_code(site, None, "1PON-100826_001"))
    assert len(calls) == 3


@pytest.mark.parametrize("title,key", [
    ("1pondo 一本道 010120_001 标题", "1PON-010120_001"),
    ("Caribbeancom 加勒比 100826-001 标题", "CARIB-100826-001"),
    ("caribbeancompr 100826_001 标题", "CARIBPR-100826_001"),
    ("10musume-100226_01 标题", "10MU-100226_01"),
    ("paco 100826_001 标题", "PACO-100826_001"),
    ("100826_001 标题", "100826_001"),
])
def test_supjav_studio_date_titles(title, key):
    assert code_key(parse_title(title)[0]) == key


def test_supjav_primary_code_not_overridden_by_date_in_title():
    assert parse_title("SSIS-001 与 100826_001 的标题")[0] == "SSIS-001"


@pytest.mark.parametrize("maker,expected", [
    ("カリビアンコム プレミアム", "CARIBPR-010120_001"),
    ("Caribbeancom Premium", "CARIBPR-010120_001"),
    (" Caribbeancom\tPremium ", "CARIBPR-010120_001"),
    ("カリビアンコム", "010120_001"),
    ("Caribbeancom", "010120_001"),
    ("CARIB", "010120_001"),
])
def test_underscore_date_maker_never_becomes_carib(maker, expected):
    assert code_key("010120_001", maker) == expected
    assert code_key("010120-001", maker) == "CARIB-010120-001"


@pytest.mark.parametrize("found,wanted,expected", [
    ("LUXU-1", "LUXU-1", True),
    ("010120_001", "1PON-010120_001", True),
    ("010120_001", "CARIBPR-010120_001", True),
    ("010120_001", "10MU-010120_001", True),
    ("010120_001", "PACO-010120_001", True),
    ("010120_001", "CARIB-010120_001", False),
    ("010120_001", "CARIB-010120-001", False),
    ("010120_001", "PACO-010120_002", False),
    ("010120_01", "10MU-010120_01", False),
    ("1PON-010120_001", "PACO-010120_001", False),
])
def test_compatible_keys_only_lift_bare_dates_and_keep_known_studios_apart(found, wanted, expected):
    assert compatible(found, wanted) is expected
    # 反方向不成立：带厂牌的不能挂到没写厂牌的作品上，否则同号的不同厂牌会被并到一起
    assert compatible(wanted, found) is (found == wanted)


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("requested,found,expected", [
    ("1PON-010120_001", "010120_001", True),
    ("010120_001", "PACO-010120_001", False),
    ("010120_001", "010120_001", True),
    ("1PON-010120_001", "PACO-010120_001", False),
    ("010120_001", "CARIB-010120-001", False),
])
def test_lookup_accepts_only_compatible_date_keys(search, requested, found, expected):
    class Fake(Site):
        lookup_verified = search

        def key_for(self, code, subtitle="", uncensored=False):
            return "detail"

        def detail_path(self, key):
            return "/detail"

        async def lookup(self, *args, **kwargs):
            return [SourceItem(key="detail", code=found, title="", uncensored=True)], "fake"

        async def fetch_detail(self, *args, **kwargs):
            return SourceDetail(key="detail", code=found, title="", uncensored=True)

    result = asyncio.run(find_by_code(Fake(), None, requested))
    assert bool(result) is expected


@pytest.mark.parametrize("stored,incoming,expected", [
    (["1PON-010120_001"], "010120_001", 1),
    (["010120_001"], "PACO-010120_001", None),
    (["1PON-010120_001", "PACO-010120_001"], "010120_001", None),
    (["010120_001", "010120_001"], "PACO-010120_001", None),
    (["PACO-010120_001", "010120_001"], "010120_001", 2),
    (["010120_001", "PACO-010120_001"], "PACO-010120_001", 2),
    (["CARIB-010120-001"], "010120_001", None),
    (["PACO-010120_001"], "1PON-010120_001", None),
])
def test_attach_exact_first_and_only_unique_compatible_work(make_store, stored, incoming, expected):
    async def run():
        db, _ = await make_store()
        for i, key in enumerate(stored, 1):
            await db._write("INSERT INTO videos(id,slug,code,code_key,uncensored,status,created_at,updated_at) VALUES(?,?,?,?,1,?,1,1)",
                            (i, f"old-{i}", key, key, "active" if i == 1 else "gone"))
        async with db._tx():
            vid, _, created, _ = await db._attach("jable", "incoming", incoming, False, "incoming", "", "", "")
        assert created is (expected is None)
        assert vid == (expected if expected else len(stored) + 1)
    asyncio.run(run())


def test_bare_date_work_does_not_absorb_studio_sources(make_store):
    """没写厂牌的源所在站点优先级最高时，作品键一直是裸日期；同号的一本道、パコパコママ各是各的片。"""
    async def run():
        db, _ = await make_store()
        order = {"supjav": 0, "missav": 1, "123av": 2}
        bare, _ = await db.upsert_item("supjav", SourceItem(key="1", code="100826_001", title="x"), "100826_001",
                                       order.get)
        pondo, _ = await db.upsert_item("missav", SourceItem(key="pondo-100826_001", code="PONDO-100826_001",
                                                             title="y"), "pondo-100826_001", order.get)
        paco, _ = await db.upsert_item("123av", SourceItem(key="paco-100826_001", code="PACO-100826_001",
                                                           title="z"), "paco-100826_001", order.get)
        assert len({bare, pondo, paco}) == 3
        assert [s["site"] for s in await db.get_sources(bare)] == ["supjav"]
        # 后来没写厂牌的源先按裸键精确匹配，回到裸日期作品，不在两个带厂牌的作品里猜
        again, created = await db.upsert_item("javguru", SourceItem(key="9", code="100826_001", title="w"),
                                              "100826_001", lambda s: 3)
        assert again == bare and not created
    asyncio.run(run())


@pytest.mark.parametrize("key", ["1PON-010120_001", "HEYZO-1234", "TOKYOHOT-N1234", "010120_001"])
def test_native_attach_uses_legacy_uncensored_rows_without_changing_them(make_store, key):
    async def run():
        db, _ = await make_store()
        await db._write("INSERT INTO videos(id,slug,code,code_key,uncensored,status,created_at,updated_at) VALUES(1,'old-u',?,?,1,'active',1,1)", (key, key))
        async with db._tx():
            vid, _, created, _ = await db._attach("jable", "native", key, False, "native", "", "", "")
        assert vid == 1 and not created
        old = await db.get_video_by_id(1)
        assert old["slug"] == "old-u" and old["uncensored"] == 1
    asyncio.run(run())


def test_key_recalculation_falls_back_to_slug(make_store):
    async def run():
        db, _ = await make_store()
        await db._write("""INSERT INTO videos(id,slug,code,code_key,maker,created_at,updated_at)
                           VALUES(1,'259luxu-1850','','259LUXU-1850','',1,1),
                                 (2,'010120_001','','010120_001','',1,1),
                                 (3,'other-slug','SSIS-001','SSIS-1','',1,1)""")
        await db.set_source_check(1, "missav", False)
        await db.set_source_check(2, "missav", False)
        async with db._tx() as conn:
            await _migrate_v22(conn)
        assert (await db.get_video_by_id(1))["code_key"] == "LUXU-1850"
        assert await db.get_source_check(1, "missav") is None
        assert await db.get_source_check(2, "missav") is not None
        await db._update_work(2, {"maker": "Caribbeancom Premium"})
        v = await db.get_video_by_id(2)
        assert v["code"] == "" and v["code_key"] == "CARIBPR-010120_001"
        await db._update_work(3, {"maker": ""})
        assert (await db.get_video_by_id(3))["code_key"] == "SSIS-1"
        await db._update_work(3, {}, "MIUM-001")
        assert (await db.get_video_by_id(3))["code_key"] == "MIUM-1"
    asyncio.run(run())
