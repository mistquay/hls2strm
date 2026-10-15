import asyncio
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from hls2strm import maintenance_scan
from hls2strm.collection_policy import merge
from hls2strm.maintenance_plan import CleanupRequest
from hls2strm.maintenance_query import AssetFilter, list_assets
from hls2strm.sites import SourceItem
from .test_maintenance import fixture, run_handler, cleanup, NFO


def test_global_exclusion_new_source_and_merge(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [1, lid]})
            job = await cleanup(e, CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove', scope='global'))
            assert not job['state'].get('failed'), await e.maintenance.items(job['id'])
            assert await db.collection_blocked(vid) and not await db.ensure_output(vid, 1)
            await db.upsert_item('missav', SourceItem(key='test-001', code='TEST-001', title='new'), 'test-001', e._rank)
            assert await db.collection_blocked(vid) and not await db.ensure_output(vid, lid)
            other, _ = await db.upsert_item('jable', SourceItem(key='test-001-u', code='TEST-001', title='U', uncensored=True), 'test-001-u', e._rank)
            assert other != vid and await db.ensure_output(other, lid)
            # 身份合并必须迁移禁收，不能在新 ID 下绕过。
            target, _ = await db.upsert_item('jable', SourceItem(key='test-002', code='TEST-002', title='target'), 'test-002', e._rank)
            async with db._tx() as conn:
                await merge(conn, vid, target)
            assert await db.collection_blocked(target) and not await db.ensure_output(target, 1)
        finally:
            await db.close()
    asyncio.run(run())


def test_move_before_journal_commit_resumes_without_overwrite(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            p = await run_handler(e, 'maintenance_preview', {'request': CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove').model_dump()})
            jid = await e.maintenance.approve(p['id'], p['state']['preview_token'])
            assert (await db.get_job(jid))['params'].get('expires_at') is None
            original = db._write
            injected = False
            async def fail(sql, args=(), **kw):
                nonlocal injected
                if not injected and sql.startswith('UPDATE cleanup_files SET state=?,error=') and args[0] == 'trashed':
                    injected = True
                    raise RuntimeError('simulated process interruption')
                return await original(sql, args, **kw)
            monkeypatch.setattr(db, '_write', fail)
            with pytest.raises(RuntimeError):
                await e.maintenance.apply(await db.get_job(jid), {})
            assert injected and await db.collection_blocked(vid, lid)
            monkeypatch.setattr(db, '_write', original)
            await e.maintenance.apply(await db.get_job(jid), {})
            job = await db.get_job(jid)
            assert not job['state'].get('failed'), await e.maintenance.items(jid)
            assert job['state']['trashed_files'] == 7
            assert await db.get_output(vid, lid) is None
        finally:
            await db.close()
    asyncio.run(run())


def test_restore_conflict_and_retention(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            job = await cleanup(e, CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove'))
            original = directory / 'TEST-001.strm'
            original.write_bytes(b'new unrelated contents')
            p = await run_handler(e, 'maintenance_restore_preview', {'restore_of': job['id']})
            assert p['state']['eligible'] == 0 and original.read_bytes() == b'new unrelated contents'
            original.unlink()
            await db.update_job(job['id'], params=json.dumps({**job['params'], 'expires_at': int(time.time()) - 1}))
            result = await e.maintenance.purge_expired()
            assert result['expired_files'] == 7 and not result['failed_files']
            assert await db.collection_blocked(vid, lid)
            p = await run_handler(e, 'maintenance_restore_preview', {'restore_of': job['id']})
            assert p['state']['eligible'] == 0
        finally:
            await db.close()
    asyncio.run(run())


def test_nfo_failure_keeps_snapshot_and_failed_scan_keeps_baseline(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            (directory / 'TEST-001.nfo').write_bytes(b'<movie>broken')
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            assets = await list_assets(db, AssetFilter(library_ids=[lid]))
            assert assets['items'][0]['nfo_status'] == 'invalid'
            assert assets['items'][0]['details']['historical_metadata']['title'] == '本地标题'
            assert (await list_assets(db, AssetFilter(library_ids=[lid], categories=['类型A'])))['total'] == 0
            original = maintenance_scan.walk
            def fail(root, *args, **kwargs):
                if root == directory.parents[1]:
                    raise PermissionError('scan unavailable')
                return original(root, *args, **kwargs)
            monkeypatch.setattr(maintenance_scan, 'walk', fail)
            job = await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            assert job['state']['failed_roots'] == 1
            page = await list_assets(db, AssetFilter(library_ids=[lid]))
            assert page['items'][0]['health'] == 'unavailable'
            p = await run_handler(e, 'maintenance_preview', {'request': CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove').model_dump()})
            assert not p['state'].get('eligible')
        finally:
            await db.close()
    asyncio.run(run())


def test_cleaned_nfo_and_poster_are_not_recreated(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await db._write("UPDATE videos SET detail_at=1,cover_url='https://fixture/cover.jpg',release_date='2015-03-02' WHERE id=?", (vid,))
            await db.ensure_output(vid, 1)
            v = await db.get_video_by_id(vid)
            await e._output_one(v, 1, cover=True)
            output = await db.get_output(vid, 1)
            strm = Path(output['strm_path'])
            poster = strm.with_name(strm.stem + '-poster.jpg')
            assert poster.exists() and strm.with_suffix('.nfo').exists()
            await run_handler(e, 'maintenance_scan', {'library_ids': [1]})
            job = await cleanup(e, CleanupRequest(query=AssetFilter(library_ids=[1]), resource_types=['nfo', 'poster'], disposition='permanent'))
            assert not job['state'].get('failed'), await e.maintenance.items(job['id'])
            assert not poster.exists() and not strm.with_suffix('.nfo').exists()
            await e._output_one(v, 1, cover=True)
            verify_id = await e.create_verify(1)
            await e._do_verify(await db.get_job(verify_id), {})
            assert not poster.exists() and not strm.with_suffix('.nfo').exists()
            assert strm.exists() and strm.with_name(strm.stem+'-fanart.jpg').exists()
        finally:
            await db.close()
    asyncio.run(run())


def test_changed_nfo_prevents_delete_and_manual_binding(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            (directory / 'TEST-001.strm').write_text('http://other.example/file', encoding='utf-8')
            await db.delete_output(vid, lid)
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            a = (await list_assets(db, AssetFilter(library_ids=[lid])))['items'][0]
            assert a['status'] == 'unmanaged'
            await e.maintenance.bind_asset(a['id'], vid)
            p = await run_handler(e, 'maintenance_preview', {'request': CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove').model_dump()})
            assert p['state']['eligible'] == 1
            (directory / 'TEST-001.nfo').write_text(NFO.replace('2015', '2022'), encoding='utf-8')
            jid = await e.maintenance.approve(p['id'], p['state']['preview_token'])
            await e.maintenance.apply(await db.get_job(jid), {})
            assert (await db.get_job(jid))['state']['failed'] == 1
            assert (directory / 'TEST-001.strm').exists()
        finally:
            await db.close()
    asyncio.run(run())


def test_located_in_external_root_is_not_missing(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            old = e.writer.library_root(e.libs[lid]) / 'TEST-001' / 'TEST-001.strm'
            await db.set_output(vid, lid, str(old))
            await run_handler(e, 'maintenance_scan', {'library_ids':[lid]})
            assert (await list_assets(db, AssetFilter(library_ids=[lid],status='missing')))['total'] == 0
            assert (await db.get_output(vid,lid))['strm_path'] == str(directory/'TEST-001.strm')
        finally:
            await db.close()
    asyncio.run(run())


def test_preserved_timestamps_do_not_hide_changed_nfo(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids':[lid]})
            p = await run_handler(e, 'maintenance_preview', {'request':CleanupRequest(query=AssetFilter(library_ids=[lid],release_to='2017-12-31'),action='remove').model_dump()})
            path = directory/'TEST-001.nfo'
            old = path.stat()
            path.write_text(NFO.replace('2015','2023'),encoding='utf-8')
            os.utime(path, ns=(old.st_atime_ns,old.st_mtime_ns))
            jid = await e.maintenance.approve(p['id'],p['state']['preview_token'])
            await e.maintenance.apply(await db.get_job(jid),{})
            assert (await db.get_job(jid))['state']['failed'] == 1
            await run_handler(e, 'maintenance_scan', {'library_ids':[lid]})
            assert (await list_assets(db,AssetFilter(library_ids=[lid],release_to='2017-12-31')))['total'] == 0
        finally:
            await db.close()
    asyncio.run(run())


def test_restore_multiple_copies_keeps_resource_policy_until_all_restored(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            shutil.copytree(directory, directory.with_name('TEST-001-copy'))
            await run_handler(e, 'maintenance_scan', {'library_ids':[lid]})
            job = await cleanup(e, CleanupRequest(query=AssetFilter(library_ids=[lid]), keep_extra=0))
            assert job['state']['done'] == 2
            p = await run_handler(e, 'maintenance_restore_preview', {'restore_of':job['id']})
            jid = await e.maintenance.approve(p['id'],p['state']['preview_token'])
            original = e.maintenance.progress
            observations = []
            async def progress(job_id, preview_id, item_id=None, before=None):
                await original(job_id, preview_id, item_id, before)
                if item_id:
                    row = await db._one("SELECT 1 FROM resource_exclusions WHERE video_id=? AND library_id=? AND kind='extra'", (vid,lid))
                    observations.append(bool(row))
            monkeypatch.setattr(e.maintenance,'progress',progress)
            await e.maintenance.restore(await db.get_job(jid),{})
            assert observations == [True,False]
            assert (await db.get_job(jid))['state']['restored'] == 2
        finally:
            await db.close()
    asyncio.run(run())
