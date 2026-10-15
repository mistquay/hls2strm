import asyncio
import json
import shutil
from pathlib import Path

import pytest

from hls2strm.nfo import parse
from hls2strm.maintenance_plan import CleanupRequest
from hls2strm.maintenance_query import AssetFilter, list_assets
from hls2strm.sites import SourceItem
from .conftest import FakeFetcher
from .test_engine import build


NFO = '''<?xml version="1.0" encoding="utf-8"?><movie><num>TEST-001</num><title>本地标题</title>
<premiered>2015-03-02</premiered><releasedate>2015-03-02</releasedate><year>2015</year>
<genre>类型A</genre><genre>类型B</genre><tag>标签甲</tag><actor><name>演员甲</name></actor>
<studio>厂牌甲</studio><runtime>90</runtime></movie>'''


def test_nfo_fields_and_security():
    values = parse(NFO.encode())
    assert values["categories"] == ["类型A", "类型B"] and values["models"] == ["演员甲"]
    assert values["duration"] == 5400 and values["release_date"] == "2015-03-02"
    assert parse(b'<movie><year>2014</year></movie>')["date_precision"] == "year"
    assert parse(NFO.replace('2015-03-02</releasedate>', '2016-03-02</releasedate>').encode())["errors"]
    attack = '<!DOCTYPE movie [<!ENTITY x "EXPAND">]><movie><title>&x;</title></movie>'
    for payload in (attack.encode(), attack.encode('utf-16')):
        with pytest.raises(ValueError, match='DTD'):
            parse(payload)
    with pytest.raises(ValueError, match='层级'):
        parse(('<movie>' + '<x>' * 40 + '</x>' * 40 + '</movie>').encode())


async def fixture(make_store, boot, tmp_path):
    db, store = await make_store()
    e = build(db, store, boot, FakeFetcher('', {}))
    await e.reload_libraries()
    e.writer.library_root(e.libs[1]).mkdir(parents=True, exist_ok=True)
    root = tmp_path / 'organized'
    root.mkdir()
    lid = (await e.create_library('整理库', 'inbox', external_dir=str(root)))["id"]
    e.writer.library_root(e.libs[lid]).mkdir(parents=True, exist_ok=True)
    vid, _ = await db.upsert_item('jable', SourceItem(key='test-001', code='TEST-001', title='源站标题'), 'test-001', e._rank)
    v = await db.get_video_by_id(vid)
    d = root / 'TEST' / 'TEST-001'
    d.mkdir(parents=True)
    s = d / 'TEST-001.strm'
    s.write_text('http://local/play/test-001.m3u8', encoding='utf-8')
    (d / 'TEST-001.nfo').write_text(NFO, encoding='utf-8')
    (d / 'poster.jpg').write_bytes(b'poster')
    (d / 'extrafanart').mkdir()
    for n in (1, 2, 3, 10):
        (d / 'extrafanart' / f'fanart{n}.jpg').write_bytes(bytes(n))
    await db.ensure_output(vid, lid)
    await db.set_output(vid, lid, str(s))
    return e, db, lid, vid, d


async def run_handler(e, kind, params):
    jid = await e.maintenance.create(kind, params)
    handler = {'maintenance_scan': e.maintenance.scan, 'maintenance_preview': e.maintenance.preview,
               'maintenance_restore_preview': e.maintenance.restore_preview}[kind]
    await handler(await e.db.get_job(jid), {})
    await e.db.update_job(jid, status='done')
    return await e.db.get_job(jid)


async def cleanup(e, request):
    preview = await run_handler(e, 'maintenance_preview', {'request': request.model_dump()})
    assert preview['state']['eligible'], await e.maintenance.items(preview['id'])
    jid = await e.maintenance.approve(preview['id'], preview['state']['preview_token'])
    await e.maintenance.apply(await e.db.get_job(jid), {})
    await e.db.update_job(jid, status='done')
    return await e.db.get_job(jid)


def test_scan_filter_resources_and_recovery(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            query = AssetFilter(library_ids=[lid], categories=['类型A'], release_to='2017-12-31')
            page = await list_assets(db, query)
            assert page['total'] == 1 and page['items'][0]['metadata']['title'] == '本地标题'
            assert (await db.get_video_by_id(vid))['title'] == '源站标题'
            assert (await list_assets(db, AssetFilter(library_ids=[lid], categories=['类型C'])))['total'] == 0
            assert (await list_assets(db, AssetFilter(library_ids=[lid], categories=['类型A', '类型B'], category_mode='all')))['total'] == 1
            job = await cleanup(e, CleanupRequest(query=query, keep_extra=2))
            assert job['state'].get('failed', 0) == 0, await e.maintenance.items(job['id'])
            assert sorted(p.name for p in (directory / 'extrafanart').glob('*.jpg')) == ['fanart1.jpg', 'fanart2.jpg']
            assert (directory / 'poster.jpg').read_bytes() == b'poster'
            assert await db.get_output(vid, lid) and not await db.collection_blocked(vid, lid)
            restore = await run_handler(e, 'maintenance_restore_preview', {'restore_of': job['id']})
            restore_id = await e.maintenance.approve(restore['id'], restore['state']['preview_token'])
            await e.maintenance.restore(await db.get_job(restore_id), {})
            assert len(list((directory / 'extrafanart').glob('*.jpg'))) == 4
        finally:
            await db.close()
    asyncio.run(run())


def test_remove_prevents_reimport_and_restore(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            job = await cleanup(e, CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove'))
            assert not job['state'].get('failed'), await e.maintenance.items(job['id'])
            assert not list(directory.rglob('*.strm'))
            assert await db.get_output(vid, lid) is None and await db.collection_blocked(vid, lid)
            await db.upsert_item('missav', SourceItem(key='test-001', code='TEST-001', title='新源标题'), 'test-001', e._rank)
            assert not await db.ensure_output(vid, lid)
            await e._output_one(await db.get_video_by_id(vid), lid, cover=False)
            assert not list(directory.rglob('*.strm'))
            # 别的库仍可收录，库级排除不自动变成全局。
            assert await db.ensure_output(vid, 1)
            restore = await run_handler(e, 'maintenance_restore_preview', {'restore_of': job['id']})
            jid = await e.maintenance.approve(restore['id'], restore['state']['preview_token'])
            await e.maintenance.restore(await db.get_job(jid), {})
            assert not (await db.get_job(jid))['state'].get('failed'), await e.maintenance.items(jid)
            assert (directory / 'TEST-001.strm').is_file() and await db.get_output(vid, lid)
            assert not await db.collection_blocked(vid, lid)
        finally:
            await db.close()
    asyncio.run(run())


def test_changed_preview_and_missing_review(make_store, boot, tmp_path):
    async def run():
        e, db, lid, vid, directory = await fixture(make_store, boot, tmp_path)
        try:
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            p = await run_handler(e, 'maintenance_preview', {'request': CleanupRequest(query=AssetFilter(library_ids=[lid]), action='remove').model_dump()})
            (directory / 'new.txt').write_text('new')
            jid = await e.maintenance.approve(p['id'], p['state']['preview_token'])
            await e.maintenance.apply(await db.get_job(jid), {})
            assert (await db.get_job(jid))['state']['failed'] == 1
            assert (directory / 'TEST-001.strm').is_file()
            await db._write("UPDATE libraries SET missing_policy='review' WHERE id=?", (lid,))
            await e.reload_libraries()
            shutil.rmtree(directory)
            assert await e.restore_lost(await db.get_video_by_id(vid), e.libs[lid]) == 'review'
            assert not directory.exists()
            await run_handler(e, 'maintenance_scan', {'library_ids': [lid]})
            request = CleanupRequest(query=AssetFilter(library_ids=[lid], status='missing'), action='reconcile', allow_empty=True)
            job = await cleanup(e, request)
            assert not job['state'].get('failed'), await e.maintenance.items(job['id'])
            assert await db.collection_blocked(vid, lid) and not await db.get_output(vid, lid)
        finally:
            await db.close()
    asyncio.run(run())
