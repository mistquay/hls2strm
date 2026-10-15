"""媒体库维护独立索引；不覆盖源站元数据。"""


async def migrate(conn):
    for sql in (
        "ALTER TABLE libraries ADD COLUMN missing_policy TEXT NOT NULL DEFAULT 'restore'",
        """CREATE TABLE maintenance_roots (
            library_id INTEGER NOT NULL, root TEXT NOT NULL, scan_id INTEGER,
            identity TEXT NOT NULL DEFAULT '[]', health TEXT NOT NULL DEFAULT 'unknown',
            error TEXT NOT NULL DEFAULT '', checked_at INTEGER NOT NULL DEFAULT 0,
            stats TEXT NOT NULL DEFAULT '{}', baseline INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(library_id,root))""",
        """CREATE TABLE library_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL, library_id INTEGER NOT NULL,
            root TEXT NOT NULL, path TEXT NOT NULL, video_id INTEGER, status TEXT NOT NULL,
            nfo_status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', effective TEXT NOT NULL DEFAULT '{}',
            data TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, extra_bytes INTEGER NOT NULL DEFAULT 0,
            extra_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(scan_id,library_id,root,path))""",
        "CREATE INDEX assets_library_scan ON library_assets(library_id,root,scan_id,id)",
        "CREATE INDEX assets_video ON library_assets(video_id,library_id,id)",
        "CREATE INDEX assets_size ON library_assets(bytes DESC,id)",
        "CREATE INDEX assets_release ON library_assets(json_extract(metadata,'$.release_date'),id)",
        """CREATE TABLE asset_facets (
            asset_id INTEGER NOT NULL, origin TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(asset_id,origin,kind,value))""",
        "CREATE INDEX asset_facets_lookup ON asset_facets(origin,kind,value,asset_id)",
        """CREATE TABLE collection_exclusions (
            video_id INTEGER NOT NULL, library_id INTEGER NOT NULL DEFAULT 0, job_id INTEGER NOT NULL,
            reason TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(video_id,library_id,job_id))""",
        """CREATE TABLE resource_exclusions (
            video_id INTEGER NOT NULL, library_id INTEGER NOT NULL, kind TEXT NOT NULL,
            job_id INTEGER NOT NULL, PRIMARY KEY(video_id,library_id,kind,job_id))""",
        """CREATE TABLE maintenance_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, preview_id INTEGER NOT NULL, video_id INTEGER,
            data TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', error TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX maintenance_items_preview ON maintenance_items(preview_id,id)",
        """CREATE TABLE cleanup_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, source TEXT NOT NULL,
            destination TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'planned', error TEXT NOT NULL DEFAULT '',
            UNIQUE(item_id,source))""",
        "CREATE INDEX cleanup_files_item ON cleanup_files(item_id,id)",
        # 最后一层写入保护，包含来源库批量 INSERT 与未来新增入口。
        """CREATE TRIGGER outputs_collection_insert BEFORE INSERT ON outputs
            WHEN EXISTS(SELECT 1 FROM collection_exclusions e WHERE e.video_id=NEW.video_id
                         AND e.library_id IN (0,NEW.library_id)) BEGIN SELECT RAISE(IGNORE); END""",
    ):
        await conn.execute(sql)
