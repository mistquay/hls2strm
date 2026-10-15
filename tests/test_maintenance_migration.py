import asyncio
import sqlite3

import aiosqlite

from hls2strm.db import Database, MIGRATIONS


def test_v26_backup_upgrade_preserves_records(tmp_path):
    async def run():
        path = tmp_path / 'old.db'
        async with aiosqlite.connect(path, isolation_level=None) as conn:
            conn.row_factory = aiosqlite.Row
            for step in MIGRATIONS[:26]:
                await step(conn)
            await conn.execute("INSERT INTO videos(id,slug,code,title,created_at,updated_at) VALUES(99,'test-099','TEST-099','keep metadata',1,1)")
            await conn.execute("PRAGMA user_version=26")
        db = Database(path)
        await db.open()
        try:
            assert (await db.get_video_by_id(99))['title'] == 'keep metadata'
            assert (await db.get_library(1))['missing_policy'] == 'restore'
            assert not await db.collection_blocked(99, 1)
            assert (await db._one('PRAGMA user_version'))[0] == len(MIGRATIONS)
            backups = list(tmp_path.glob('old.db.pre-v27-*.bak'))
            assert len(backups) == 1
            connection = sqlite3.connect(backups[0])
            try:
                assert connection.execute('PRAGMA user_version').fetchone()[0] == 26
                assert connection.execute('SELECT title FROM videos WHERE id=99').fetchone()[0] == 'keep metadata'
            finally:
                connection.close()
        finally:
            await db.close()
    asyncio.run(run())
