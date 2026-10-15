import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from hls2strm.sites import SourceItem
from .test_auth import _client, _cleanup
from .test_maintenance import NFO


def test_authenticated_scan_preview_execute_and_restore(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            assert client.get('/api/maintenance/overview').status_code == 401
            assert client.post('/api/maintenance/preview', json={}).status_code == 401
            client.post('/api/login', json={'username':'admin','password':'pw'})
            async def seed():
                e = app.state.ctx.engine
                vid, _ = await e.db.upsert_item('jable', SourceItem(key='test-001',code='TEST-001',title='source'), 'test-001', e._rank)
                root = e.writer.library_root(e.libs[1]) / 'TEST-001'
                root.mkdir(parents=True)
                (root / 'TEST-001.strm').write_text('http://localhost/play/test-001.m3u8', encoding='utf-8')
                (root / 'TEST-001.nfo').write_text(NFO, encoding='utf-8')
                (root / 'extrafanart').mkdir()
                (root / 'extrafanart' / 'fanart1.jpg').write_bytes(b'fixture')
                await e.db.ensure_output(vid, 1)
                await e.db.set_output(vid, 1, str(root / 'TEST-001.strm'))
                return root
            root = client.portal.call(seed)
            def wait(jid):
                for _ in range(300):
                    data = client.get(f'/api/maintenance/jobs/{jid}').json()
                    if data['job']['status'] not in ('running', 'paused'):
                        assert not data['job']['state'].get('partial_failure'), data
                        return data
                    time.sleep(.02)
                raise AssertionError('maintenance job timeout')
            jid = client.post('/api/maintenance/scan',json={'library_ids':[1]}).json()['job_id']
            wait(jid)
            page = client.post('/api/maintenance/assets/query', json={'categories':['类型A'],'release_to':'2017-12-31'}).json()
            assert page['total'] == 1
            assert client.put('/api/maintenance/libraries/1/policy', json={'missing_policy':'review'}).status_code == 200
            body = {'query':{'library_ids':[1],'categories':['类型A']},'action':'resources','keep_extra':0,'disposition':'trash'}
            response = client.post('/api/maintenance/preview',json=body,headers={'Idempotency-Key':'maintenance-api'})
            preview = response.json()['job_id']
            assert client.post('/api/maintenance/preview',json=body,headers={'Idempotency-Key':'maintenance-api'}).json()['job_id'] == preview
            data = wait(preview)
            assert data['job']['state']['eligible'] == 1
            assert (root/'extrafanart'/'fanart1.jpg').exists()
            assert client.post(f'/api/maintenance/jobs/{preview}/apply', json={'preview_token':'bad'}).status_code == 409
            token = {'preview_token':data['job']['state']['preview_token']}
            jid = client.post(f'/api/maintenance/jobs/{preview}/apply',json=token).json()['job_id']
            assert client.post(f'/api/maintenance/jobs/{preview}/apply',json=token).json()['job_id'] == jid
            result = wait(jid)
            files = client.get(f"/api/maintenance/items/{result['items'][0]['id']}/files").json()
            assert files['total'] == 1 and files['items'][0]['state'] == 'trashed'
            assert not (root/'extrafanart'/'fanart1.jpg').exists()
            restore_id = client.post(f'/api/maintenance/jobs/{jid}/restore-preview').json()['job_id']
            data = wait(restore_id)
            jid2 = client.post(f'/api/maintenance/jobs/{restore_id}/apply',json={'preview_token':data['job']['state']['preview_token']}).json()['job_id']
            wait(jid2)
            assert (root/'extrafanart'/'fanart1.jpg').read_bytes() == b'fixture'
    finally:
        _cleanup()
