"""十万资产 / 百万清单条目的查询验收，不冒充真实磁盘扫描测试。"""

import asyncio
import json
import time

from hls2strm.maintenance_query import AssetFilter, list_assets


def test_large_index_filters_without_filesystem(make_store, boot, monkeypatch):
    async def run():
        db, _ = await make_store()
        try:
            async with db._tx() as conn:
                await conn.execute("""WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<100000)
                    INSERT INTO videos(id,slug,code,code_key,title,created_at,updated_at)
                    SELECT n,'scale-'||n,'SCALE-'||n,'SCALE-'||n,'fixture',1,1 FROM seq""")
                await conn.execute("INSERT INTO maintenance_roots(library_id,root,scan_id,health) VALUES(1,'fixture',1,'ok')")
                manifest = json.dumps({'files':[{'path':f'fanart{i}.jpg','size':1024,'kind':'extra'} for i in range(10)]})
                await conn.execute("""INSERT INTO library_assets(id,scan_id,library_id,root,path,video_id,status,nfo_status,metadata,effective,data,bytes,extra_bytes,extra_count)
                    SELECT id,1,1,'fixture',slug,id,'present','valid',
                    json_object('title',title,'release_date',CASE WHEN id%2=0 THEN '2015-01-01' ELSE '2023-01-01' END,'categories',json('["类型A"]')),
                    '{}',?,10240,10240,10 FROM videos""", (manifest,))
                await conn.execute("INSERT INTO asset_facets SELECT id,'nfo','categories','类型A' FROM library_assets")
            def unexpected(*args, **kwargs):
                raise AssertionError('查询不能扫描磁盘')
            monkeypatch.setattr('hls2strm.maintenance_files.walk', unexpected)
            started = time.perf_counter()
            result = await list_assets(db, AssetFilter(categories=['类型A'], release_to='2017-12-31'))
            elapsed = time.perf_counter() - started
            assert result['total'] == 50000 and len(result['items']) == 50
            assert result['extra_bytes'] == 50000 * 10240
            assert all(item['file_count'] == 10 for item in result['items'])
            print(f'Maintenance index: 100000 assets / 1000000 manifest entries; filter + count + first page: {elapsed:.3f}s')
        finally:
            await db.close()
            # 大容量合成数据库只用于本次测试，避免给用户留下大量占用。
            db.path.unlink(missing_ok=True)
    asyncio.run(run())
