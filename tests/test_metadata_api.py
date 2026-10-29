import time

from fastapi.testclient import TestClient

from hls2strm.sites import SourceItem
from .test_auth import _client, _cleanup


def test_authenticated_preview_apply_history_and_protected_records(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            assert client.get("/api/metadata/jobs").status_code == 401
            assert client.post("/api/metadata/preview", json={}).status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})

            async def seed():
                e = app.state.ctx.engine
                root = tmp_path / "organized"
                root.mkdir()
                lid = (await e.create_library("External", "inbox", external_dir=str(root)))["id"]
                e.writer.library_root(e.libs[lid]).mkdir(parents=True, exist_ok=True)
                vid, _ = await e.db.upsert_item("jable", SourceItem(key="test-001", code="TEST-001", title="Source title"), "test-001", e._rank)
                folder = root / "TEST-001"
                folder.mkdir()
                strm = folder / "TEST-001.strm"
                strm.write_text("http://localhost/play/test-001.m3u8", encoding="utf-8")
                await e.db.ensure_output(vid, lid)
                await e.db.set_output(vid, lid, str(strm))
                return lid, folder

            lid, folder = client.portal.call(seed)

            def wait(jid):
                for _ in range(300):
                    result = client.get(f"/api/metadata/jobs/{jid}").json()
                    if result["job"]["status"] not in ("running", "paused"):
                        return result
                    time.sleep(.02)
                raise AssertionError("metadata job timeout")

            body = {"library_id": lid, "mode": "missing", "images": False}
            p = client.post("/api/metadata/preview", json=body, headers={"Idempotency-Key": "metadata-preview"})
            assert p.status_code == 202, p.text
            pid = p.json()["job_id"]
            assert client.post("/api/metadata/preview", json=body, headers={"Idempotency-Key": "metadata-preview"}).json()["job_id"] == pid
            data = wait(pid)
            assert data["job"]["state"].get("eligible") == 1, data
            assert not (folder / "TEST-001.nfo").exists()
            assert data["items"][0]["changes"]
            token = {"preview_token": data["job"]["state"]["preview_token"], "scraper_paused": True}
            assert client.post(f"/api/metadata/jobs/{pid}/apply", json={**token, "scraper_paused": False}).status_code == 409
            assert client.post(f"/api/metadata/jobs/{pid}/apply", json={**token, "preview_token": "wrong"}).status_code == 409
            jid = client.post(f"/api/metadata/jobs/{pid}/apply", json=token).json()["job_id"]
            assert client.post(f"/api/metadata/jobs/{pid}/apply", json=token).json()["job_id"] == jid
            result = wait(jid)
            assert result["job"]["state"].get("done") == 1, result
            assert result["items"][0]["state"] == "done"
            assert (folder / "TEST-001.nfo").exists()
            assert client.get("/api/metadata/jobs?limit=1").json()["total"] == 2
            assert client.delete(f"/api/jobs/{pid}").status_code == 400
            assert client.delete(f"/api/jobs/{jid}").status_code == 400
            assert client.post(f"/api/jobs/{jid}/retry").status_code == 400
            assert client.post(f"/api/metadata/jobs/{jid}/retry", json={"preview_token": "", "scraper_paused": False}).status_code == 409
            assert client.post("/api/metadata/preview", json={**body, "mode": "all"}).status_code == 422
            assert client.post("/api/metadata/preview", json={**body, "library_id": 1}).status_code == 422
    finally:
        _cleanup()
