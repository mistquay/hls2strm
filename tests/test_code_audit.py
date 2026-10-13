import asyncio
import csv
import json
import os
import sqlite3
import subprocess
import sys

import aiosqlite
import pytest

from hls2strm import code_audit, codes
from hls2strm.db import MIGRATIONS


@pytest.mark.parametrize("spellings,maker,key", [
    (["259LUXU-1850", "LUXU-1850", "luxu1850", " ２５９ＬＵＸＵ－１８５０ "], "", "LUXU-1850"),
    (["300MIUM-001", "MIUM-1"], "", "MIUM-1"),
    (["T28-001"], "", "T28-1"),
    (["KIRA-020-2"], "", "KIRA-20-2"),
    (["3DSVR-0123"], "", "3DSVR-123"),
    (["12ABC-001"], "", "12ABC-1"),
    (["1000GIRI-001"], "", "1000GIRI-1"),
    (["SSIS-001", "ssis00001"], "", "SSIS-1"),
    (["FC2-PPV-1066192", "fc2ppv-1066192", "FC2-1066192", "fc2_ppv_1066192"], "", "FC2PPV-1066192"),
    (["PONDO-100826_001", "1pon-100826_001", "1pondo_100826_001"], "", "1PON-100826_001"),
    (["1pondo-010120_001"], "", "1PON-010120_001"),
    (["carib-100826-001", "caribbeancom 100826-001", "100826-001", "100826 001"], "", "CARIB-100826-001"),
    (["MUSUME-100226_01", "10mu-100226_01", "10musume 100226 01", "100226_01"], "", "10MU-100226_01"),
    (["caribpr-100826_001", "caribbeancompr 100826-001"], "", "CARIBPR-100826_001"),
    (["paco-100826_001", "pacopacomama_100826_001"], "", "PACO-100826_001"),
    (["100826_001"], "", "100826_001"),
    (["100826_001"], "不认识的厂商", "100826_001"),
    (["100826_001"], "一本道", "1PON-100826_001"),
    (["100826_001"], "Studio 1PoNdO", "1PON-100826_001"),
    (["100826_001"], "カリビアンコムプレミアム", "CARIBPR-100826_001"),
    (["100826_001"], "CaribbeancomPR", "CARIBPR-100826_001"),
    (["100826_001"], "パコパコママ", "PACO-100826_001"),
    (["100826_001"], "pacopacomama", "PACO-100826_001"),
    (["heyzo_hd_1234", "HEYZO-1234"], "", "HEYZO-1234"),
    (["HEYZO-HD-001"], "", "HEYZO-1"),
    (["tokyo-hot-n1234", "n1234"], "", "TOKYOHOT-N1234"),
    (["TOKYO HOT K 0001", "k0001"], "", "TOKYOHOT-K0001"),
    (["010120_001", "０１０１２０＿００１"], "", "010120_001"),
    (["123 45"], "", "123-45"),
    (["", "　 "], "", ""),
])
def test_audit_v2_keys(spellings, maker, key):
    assert {codes.code_key_v2(s, maker) for s in spellings} == {key}


def test_v2_editions_and_prefix_exception(monkeypatch):
    for code in ("PONDO-100826_001", "100226_01", "100826_001", "100826-001", "HEYZO-1234", "n1234"):
        assert codes.is_native(codes.code_key_v2(code))
        assert not codes.edition(code, True)
    for code in ("259LUXU-1850", "FC2-PPV-1066192"):
        assert codes.edition(code, True) and not codes.edition(code, False)
    assert codes.code_key_v2("caribpr-100826_001") != codes.code_key_v2("1pon-100826_001")
    monkeypatch.setattr(codes, "KEEP_PREFIX", {"259LUXU"})
    assert codes.code_key_v2("259LUXU-1850") == "259LUXU-1850"
    # 历史迁移和审计样本仍可重建旧键。
    assert codes.legacy_code_key("300MIUM-001") == "300MIUM-1"
    assert codes.legacy_code_key("100826-001") == "100826-001"


@pytest.fixture
def audit_db(tmp_path):
    path = tmp_path / "审计 #% 副本.db"

    async def create():
        async with aiosqlite.connect(path) as conn:
            conn.row_factory = aiosqlite.Row
            for step in MIGRATIONS[:21]:
                await step(conn)
            await conn.execute("PRAGMA user_version=21")
            await conn.commit()

    asyncio.run(create())
    with sqlite3.connect(path) as conn:
        # 三条示例归成两个版本；removed、gone 也列出，审计不能只看活动影片。
        rows = [
            (1, "259luxu-1850-u", "259LUXU-1850", 1, "", "active", 1, "2020-01-01", 1800),
            (2, "luxu-1850-u", "LUXU-1850", 1, "", "removed", 1, "2020-01-02", 2400),
            (3, "luxu-1850", "LUXU-1850", 0, "", "active", 1, "2020-01-01", 1800),
            (4, "pondo-100826_001", "PONDO-100826_001", 0, "", "active", 1, "2020-01-01", 3600),
            (5, "100826_001-u", "100826_001", 1, "一本道", "gone", None, "2021-01-01", 1200),
            (6, "unknown", "", 0, "", "active", None, "", None),
            (7, "unknown-u", "", 1, "", "active", None, "", None),
            (8, "caribpr-100826_001", "caribpr-100826_001", 0, "", "active", 1, "", None),
        ]
        conn.executemany("""INSERT INTO videos(id,slug,code,uncensored,maker,status,detail_at,release_date,duration,
                                               code_key,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,1,1)""",
                         [(*row, codes.legacy_code_key(row[2])) for row in rows])
        conn.execute("UPDATE libraries SET name='整理库',external_dir='/media/organized' WHERE id=1")
        conn.execute("INSERT INTO libraries(id,name,dir,external_dir,created_at) VALUES(2,'相对路径库','inbox','organized',1)")
        conn.executemany("INSERT INTO outputs(video_id,library_id,strm_path) VALUES(?,?,?)", [
            (1, 1, "/media/organized/LUXU/259LUXU.strm"),
            (1, 2, "/media/organized/LUXU/259LUXU.strm"),
            (2, 1, "/media/organized-other/LUXU.strm"),
            (4, 1, ""),
        ])
        conn.execute("INSERT INTO sources(video_id,site,key,status,created_at,updated_at) VALUES(2,'jable','luxu','removed',1,1)")
    return path


def test_audit_v21_read_only_and_no_file_scan(audit_db, monkeypatch):
    original = audit_db.read_bytes(), audit_db.stat().st_mtime_ns
    connect = sqlite3.connect
    calls = []

    def readonly(database, **kw):
        calls.append((database, kw))
        conn = connect(database, **kw)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE videos SET status='active'")
        conn.rollback()
        return conn

    def no_scan(*args, **kwargs):
        pytest.fail("只读审计不能扫描媒体文件")

    monkeypatch.setattr(sqlite3, "connect", readonly)
    monkeypatch.setattr(os, "scandir", no_scan)
    monkeypatch.setattr(os, "walk", no_scan)
    report = code_audit.audit(audit_db)
    assert calls == [(audit_db.resolve().as_uri() + "?mode=ro", {"uri": True})]
    assert (audit_db.read_bytes(), audit_db.stat().st_mtime_ns) == original
    assert report["schema_version"] == 21 and report["videos"] == 8
    assert report["changed_keys"] == 4 and report["sibling_videos"] == 3
    assert report["conflict_groups"] == 1 and report["unknown_external_paths"] == 1
    native, leaked = report["duplicate_groups"]
    assert (native["code_key"], native["uncensored"]) == ("1PON-100826_001", False)
    assert [v["id"] for v in native["members"]] == [4, 5] and not native["conflicts"]
    assert native["members"][1]["outputs"] == []
    assert (leaked["code_key"], leaked["uncensored"]) == ("LUXU-1850", True)
    assert leaked["conflicts"][0]["video_ids"] == [1, 2]
    assert len(leaked["conflicts"][0]["reasons"]) == 2
    assert [o["in_external"] for v in leaked["members"] for o in v["outputs"]] == [True, None, False]
    with connect(audit_db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
        assert conn.execute("SELECT status FROM sources").fetchone()[0] == "removed"


@pytest.mark.parametrize("a,b,expected", [(600, 720, False), (600, 721, True), (3600, 4000, False),
                                        (3599, 4000, True), (None, 5000, False), (0, 5000, False)])
def test_conflict_duration_threshold(a, b, expected):
    members = [{"id": i, "detail_at": 1, "release_date": "", "duration": duration}
               for i, duration in enumerate((a, b))]
    assert bool(code_audit.conflicts(members)) == expected


def test_conflict_dates_require_details_and_known_dates():
    members = [{"id": 1, "detail_at": 1, "release_date": "2020-01-01", "duration": None},
               {"id": 2, "detail_at": 1, "release_date": "2020-01-02", "duration": None}]
    assert code_audit.conflicts(members)[0]["reasons"] == ["发行日期不同"]
    members[1]["detail_at"] = None
    assert not code_audit.conflicts(members)
    members[1].update(detail_at=1, release_date="")
    assert not code_audit.conflicts(members)


@pytest.mark.parametrize("path,external,root,expected", [
    ("/media/organized/x.strm", "/media/organized", "", True),
    ("/media/organized2/x.strm", "/media/organized", "", False),
    ("/media/organized/../inbox/x.strm", "/media/organized", "", False),
    ("/media/organized/x.strm", "organized", "/media", True),
    ("/media/organized/x.strm", "organized", "", None),
    ("/media/organized/x.strm", "organized", "relative-root", None),
    ("D:/MEDIA/organized/x.strm", "d:\\media\\organized", "", True),
    ("D:/media/organized/x.strm", "organized", "d:\\media", True),
    ("//server/share/organized/x.strm", "\\\\server\\share\\organized", "", True),
    ("inbox/x.strm", "/media/organized", "", None),
    ("", "/media/organized", "", False),
    ("/media/x.strm", "", "", False),
])
def test_recorded_external_paths(path, external, root, expected):
    assert code_audit.recorded_external(path, external, root) is expected


def test_cli_csv_without_application_dependencies(audit_db, tmp_path):
    out = tmp_path / "groups.csv"
    result = subprocess.run([sys.executable, "-X", "utf8", "-S", "-m", "hls2strm.code_audit",
                             "--db", str(audit_db), "--out", str(out)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    for summary in ("键变化的影片数：4", "同版重复组：2", "疑似冲突组：1", "有兄弟版本的作品数：3"):
        assert summary in result.stdout
    assert "无法判断" in result.stdout and "未归入输出库" in result.stdout
    assert not out.read_bytes().startswith(b"\xef\xbb\xbf")
    with out.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 5 and {row["video_id"] for row in rows} == {"1", "2", "4", "5"}
    assert any(row["slug"] == "100826_001-u" and row["library_id"] == "" for row in rows)
    assert json.loads(next(row["conflicts"] for row in rows if row["video_id"] == "1"))[0]["video_ids"] == [1, 2]
    original = out.read_bytes()
    with pytest.raises(SystemExit) as error:
        code_audit.main(["--db", str(audit_db), "--out", str(out)])
    assert error.value.code == 1 and out.read_bytes() == original


def test_empty_database_and_missing_input(audit_db, tmp_path):
    with sqlite3.connect(audit_db) as conn:
        conn.execute("DELETE FROM outputs")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM videos")
    report = code_audit.audit(audit_db)
    assert report["videos"] == report["changed_keys"] == report["sibling_videos"] == report["conflict_groups"] == 0
    assert report["duplicate_groups"] == []
    missing = tmp_path / "missing.db"
    with pytest.raises(SystemExit) as error:
        code_audit.main(["--db", str(missing)])
    assert error.value.code == 1 and not missing.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_csv_cannot_overwrite_database_files(audit_db, suffix):
    before = audit_db.read_bytes()
    with pytest.raises(SystemExit) as error:
        code_audit.main(["--db", str(audit_db), "--out", str(audit_db) + suffix])
    assert error.value.code == 1 and audit_db.read_bytes() == before


def test_audit_reads_committed_wal(audit_db):
    with sqlite3.connect(audit_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE videos SET code='LUXU-1851' WHERE id=2")
        conn.commit()
        # 保持写连接打开，修改仍在 WAL；不能用 immutable 忽略这些已提交的数据。
        report = code_audit.audit(audit_db)
        assert report["sibling_videos"] == 2 and len(report["duplicate_groups"]) == 1
