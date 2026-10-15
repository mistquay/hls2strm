import asyncio
import shutil
import threading

import pytest

from hls2strm import maintenance_scan, job_views
from hls2strm.engine import TaskStopped
from hls2strm.maintenance_query import AssetFilter, list_assets
from hls2strm.maintenance_plan import CleanupRequest
from .test_maintenance import fixture, run_handler


def test_external_root_only_and_unavailable_never_falls_back(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            inbox = e.writer.library_root(e.libs[lid])
            shutil.rmtree(inbox)
            original = maintenance_scan.walk
            visited = []
            def record(root, *args, **kwargs):
                visited.append(root)
                return original(root, *args, **kwargs)
            monkeypatch.setattr(maintenance_scan, 'walk', record)
            assert e.maintenance.roots(e.libs[1]) == [e.writer.library_root(e.libs[1]).absolute()]
            job = await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            assert visited == [directory.parents[1]]
            assert not job['state']['partial_failure']
            assert job['state']['assets'] == 1
            assert (await list_assets(db, AssetFilter(library_ids=[lid])))['total'] == 1
            directory.parents[1].rename(tmp_path / 'offline')
            failed = await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            assert failed['state']['failed_roots'] == 1
            assert failed['state']['root_errors'][0]['root'] == str(tmp_path / 'organized')
            assert visited == [tmp_path / 'organized']
            assert (await db.get_output(vid, lid))['strm_path'] == str(directory / 'TEST-001.strm')
            page = await list_assets(db, AssetFilter(library_ids=[lid]))
            assert page['items'][0]['health'] == 'unavailable'
        finally:
            await db.close()
    asyncio.run(run())


def test_scan_submission_is_atomic_and_paused_job_is_reused(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, _ = await fixture(make_store, boot, tmp_path)
        try:
            ids = await asyncio.gather(*(e.maintenance.create('maintenance_scan', {'library_ids': [lid]}) for _ in range(8)))
            assert len(set(ids)) == 1
            assert (await db._one('SELECT COUNT(*) AS n FROM tasks'))['n'] == 1
            jid = ids[0]
            assert (await db.get_job(jid))['state']['scan_phase'] == 'queued'
            await db.update_job(jid, status='paused')
            assert await e.maintenance.create('maintenance_scan', {'library_ids': [lid]}) == jid
            for params in ({'library_ids': [1]}, {'library_ids': [lid], 'force': True}):
                with pytest.raises(ValueError, match=f'#{jid}'):
                    await e.maintenance.create('maintenance_scan', params)
            await db.update_job(jid, status='cancelled')
            assert await e.maintenance.create('maintenance_scan', {'library_ids': [lid]}) != jid
        finally:
            await db.close()
    asyncio.run(run())


def test_waiting_scan_can_cancel_without_waiting_for_other_scan(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, _ = await fixture(make_store, boot, tmp_path)
        await e.maintenance.scan_lock.acquire()
        task = None
        try:
            jid = await e.maintenance.create('maintenance_scan', {'library_ids': [lid]})
            task = asyncio.create_task(e.maintenance.scan(await db.get_job(jid), {}))
            for _ in range(100):
                if (await db.get_job(jid))['state'].get('scan_phase') == 'waiting_lock':
                    break
                await asyncio.sleep(.01)
            job = await job_views.details(e, await db.get_job(jid))
            assert job['phase'] == 'queued' and '等待前一个扫描' in job['phase_detail']
            await db.update_job(jid, status='cancelled')
            with pytest.raises(TaskStopped):
                await asyncio.wait_for(task, 2)
            assert e.maintenance.scan_lock.locked()
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            e.maintenance.scan_lock.release()
            await db.close()
    asyncio.run(run())


def test_slow_first_batch_reports_path_before_assets_exist(make_store, boot, tmp_path, monkeypatch):
    async def run():
        e, db, lid, _, directory = await fixture(make_store, boot, tmp_path)
        release = threading.Event()
        reports = []
        original_walk, original_update = maintenance_scan.walk, db.update_job
        def slow_walk(root, *args, **kwargs):
            kwargs['visiting'](directory)
            assert release.wait(5), 'progress was never published during filesystem read'
            yield from original_walk(root, *args, **kwargs)
        async def update(jid, **fields):
            state = fields.get('state', {})
            reports.append(dict(state))
            await original_update(jid, **fields)
            if state.get('current_path') == str(directory) and not state.get('assets'):
                release.set()
        monkeypatch.setattr(maintenance_scan, 'walk', slow_walk)
        monkeypatch.setattr(db, 'update_job', update)
        try:
            job = await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            assert release.is_set()
            assert job['state']['scan_phase'] == 'done' and job['state']['assets'] == 1
            assert job['state']['outputs_checked'] == 1
            assert {s.get('scan_phase') for s in reports} >= {'preparing', 'walking', 'trash', 'reconciling', 'publishing', 'archive', 'pruning', 'done'}
        finally:
            release.set()
            await db.close()
    asyncio.run(run())


def test_retired_inbox_assets_do_not_enter_preview_and_live_copies_block_removal(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            asset = await db._one('SELECT * FROM library_assets WHERE video_id=?', (vid,))
            inbox = e.writer.library_root(e.libs[lid])
            await db._write("INSERT INTO maintenance_roots(library_id,root,scan_id,health) VALUES(?,?,?,'ok')", (lid, str(inbox), asset['scan_id']))
            await db._write('''INSERT INTO library_assets(scan_id,library_id,root,path,video_id,status,nfo_status,data)
                VALUES(?,?,?,?,?,'present','valid',?)''', (asset['scan_id'], lid, str(inbox), asset['path'], vid, asset['data']))
            await e.maintenance.refresh_roots()
            assert (await list_assets(db, AssetFilter(library_ids=[lid])))['total'] == 1
            request = {'request': CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove').model_dump()}
            preview = await run_handler(e, 'maintenance_preview', request)
            assert preview['state']['eligible'] == 1
            shutil.copytree(directory, inbox / 'copy')
            preview = await run_handler(e, 'maintenance_preview', request)
            assert not preview['state'].get('eligible')
            assert await db.get_output(vid, lid)
        finally:
            await db.close()
    asyncio.run(run())
