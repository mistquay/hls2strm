import time

from fastapi.testclient import TestClient

from hls2strm.sites import SourceItem
from .test_auth import _client, _cleanup
from .test_maintenance import NFO


def test_authenticated_unavailable_preview_and_archive(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            body = {"action": "unavailable", "disposition": "archive", "query": {"library_ids": [1]}}
            assert client.post("/api/maintenance/preview", json=body).status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            async def seed():
                e = app.state.ctx.engine
                vid, _ = await e.db.upsert_item("jable", SourceItem(key="test-001", code="TEST-001", title="residue"), "test-001", e._rank)
                directory = e.writer.library_root(e.libs[1]) / "TEST-001"
                directory.mkdir(parents=True)
                (directory / "renamed-cd2.strm").write_text("http://local/play/test-001.m3u8", encoding="utf-8")
                (directory / "TEST-001.nfo").write_text(NFO, encoding="utf-8")
                (directory / "poster.jpg").write_bytes(b"poster")
                await e.db.mark_source_gone("jable", "test-001")
                return directory
            directory = client.portal.call(seed)
            assert client.post("/api/maintenance/preview", json={**body, "disposition": "permanent"}).status_code == 422
            response = client.post("/api/maintenance/preview", json=body)
            assert response.status_code == 202, response.text
            def wait(jid):
                for _ in range(300):
                    result = client.get(f"/api/maintenance/jobs/{jid}").json()
                    if result["job"]["status"] not in ("running", "paused"):
                        assert not result["job"]["state"].get("partial_failure"), result
                        return result
                    time.sleep(.02)
                raise AssertionError("unavailable maintenance timeout")
            plan = wait(response.json()["job_id"])
            assert plan["job"]["state"]["eligible"] == 1
            assert (directory / "renamed-cd2.strm").is_file()
            files = client.get(f"/api/maintenance/items/{plan['items'][0]['id']}/files").json()
            assert files["total"] == 3 and all(f["destination"] for f in files["items"])
            url = f"/api/maintenance/jobs/{plan['job']['id']}/apply"
            assert client.post(url, json={"preview_token": "bad"}).status_code == 409
            confirm = {"preview_token": plan["job"]["state"]["preview_token"]}
            jid = client.post(url, json=confirm).json()["job_id"]
            assert client.post(url, json=confirm).json()["job_id"] == jid
            result = wait(jid)
            assert result["job"]["state"]["trashed_files"] == 3
            assert not list(directory.glob("*"))
    finally:
        _cleanup()
