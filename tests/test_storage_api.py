import time

from fastapi.testclient import TestClient

from .test_auth import _client, _cleanup
from .test_storage import movie


def test_storage_api_auth_preview_and_confirm(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            base = "/api/libraries/1/storage"
            assert client.get(base).status_code == 401
            assert client.post(base, json={"keep": 0}).status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            root = app.state.ctx.engine.storage.root(1)
            _, extra = movie(root)
            assert client.post(base, json={"keep": -1}).status_code == 422
            assert client.get(base).json() == {"job_id": None}

            def wait(job_id):
                for _ in range(200):
                    response = client.get(f"{base}/{job_id}")
                    assert response.status_code == 200
                    job = response.json()
                    if job["status"] != "running":
                        return job
                    time.sleep(0.02)
                raise AssertionError("目录任务未完成")

            headers = {"Idempotency-Key": "storage-scan-test"}
            response = client.post(base, json={"keep": 0}, headers=headers)
            assert response.status_code == 202
            scan_id = response.json()["job_id"]
            assert client.post(base, json={"keep": 0}, headers=headers).json()["job_id"] == scan_id
            scan = wait(scan_id)
            assert scan["state"]["summary"]["counts"]["delete_files"] == 5
            assert client.get(base).json()["job_id"] == scan_id
            assert len(list(extra.glob("*.jpg"))) == 5
            assert client.post(base + "/clean", json={"preview_id": scan_id, "preview_token": "wrong"}).status_code == 409
            body = {"preview_id": scan_id, "preview_token": scan["state"]["preview_token"]}
            job_id = client.post(base + "/clean", json=body).json()["job_id"]
            assert client.post(base + "/clean", json=body).json()["job_id"] == job_id
            assert wait(job_id)["state"]["deleted"] == 5
            assert not list(extra.glob("*.jpg"))
            assert client.delete(f"/api/jobs/{scan_id}").status_code == 400
            assert client.delete(f"/api/jobs/{job_id}").status_code == 200
            assert client.delete(f"/api/jobs/{scan_id}").status_code == 200
            assert not app.state.ctx.engine.storage.manifest(scan_id).exists()
    finally:
        _cleanup()
