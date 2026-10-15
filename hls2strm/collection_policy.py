"""收录与资源写入统一保护；保留来源状态和影片身份。"""


async def blocked(db, video_id, library_id=0):
    return bool(await db._one("SELECT 1 FROM collection_exclusions WHERE video_id=? AND library_id IN (0,?) LIMIT 1",
                              (video_id, library_id)))


async def resources(db, video_id, library_id):
    return {row["kind"] for row in await db._all("SELECT kind FROM resource_exclusions WHERE video_id=? AND library_id=?",
                                               (video_id, library_id))}


async def merge(conn, loser, keep):
    for table, fields in (("collection_exclusions", "library_id,job_id,reason,created_at"),
                          ("resource_exclusions", "library_id,kind,job_id")):
        await conn.execute(f"INSERT OR IGNORE INTO {table}(video_id,{fields}) SELECT ?,{fields} FROM {table} WHERE video_id=?", (keep, loser))
        await conn.execute(f"DELETE FROM {table} WHERE video_id=?", (loser,))
    await conn.execute("UPDATE library_assets SET video_id=? WHERE video_id=?", (keep, loser))
    await conn.execute("UPDATE maintenance_items SET video_id=? WHERE video_id=?", (keep, loser))
