from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from hls2strm import tidy_plans
from .test_auth import _client, _cleanup
from .test_storage import movie
from .test_trash import age, scraped


def wait_tidy(client, job_id):
    for _ in range(500):
        response = client.get(f"/api/tidy/{job_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["job"]["status"] not in ("running", "paused"):
            assert not result["job"]["tasks"].get("failed"), result
            return result
        time.sleep(.02)
    raise AssertionError("tidy job timeout")


async def library(app, root):
    root.mkdir()
    e = app.state.ctx.engine
    return (await e.create_library("organized", "inbox", external_dir=str(root)))["id"]


def test_tidy_full_preview_paging_and_frozen_apply(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            assert client.get("/api/tidy/1").status_code == 401
            assert client.post("/api/tidy/1/apply", json={"preview_token": "x"}).status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            root = tmp_path / "organized"
            lid = client.portal.call(library, app, root)
            for n in range(241):
                scraped(root / f"OLD-{n:03}", f"OLD-{n:03}")
            age(root)
            preview_id = client.post(f"/api/libraries/{lid}/tidy?apply=false").json()["job_id"]
            plan = wait_tidy(client, preview_id)
            assert plan["available"] and not plan["legacy"] and plan["total"] == 241
            assert plan["job"]["state"]["moved"] == 0
            page = client.get(f"/api/tidy/{preview_id}?offset=200&limit=50").json()
            assert len(page["items"]) == 41 and page["total"] == 241
            assert all(Path(row["path"]).is_dir() for row in page["items"])
            assert client.get(f"/api/tidy/{preview_id}?limit=201").status_code == 422
            assert client.post(f"/api/tidy/{preview_id}/apply", json={"preview_token": "wrong"}).status_code == 409
            # Newly discovered leftovers must not be added to the old preview.
            new = scraped(root / "NEW-999", "NEW-999")
            age(new)
            (root / "OLD-000" / "OLD-000.nfo").write_text("new metadata", encoding="utf-8")
            (root / "OLD-001" / "OLD-001.strm").write_text("http://local/play/restored.m3u8", encoding="utf-8")
            body = {"preview_token": plan["preview_token"]}
            response = client.post(f"/api/tidy/{preview_id}/apply", json=body)
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            assert client.post(f"/api/tidy/{preview_id}/apply", json=body).json()["job_id"] == job_id
            result = wait_tidy(client, job_id)
            assert result["job"]["params"]["preview_id"] == preview_id
            assert result["job"]["state"]["moved"] == 239
            assert result["job"]["state"]["skipped"] == 2
            assert new.is_dir() and (root / "OLD-000").is_dir() and (root / "OLD-001").is_dir()
            assert result["items"][0]["state"] == "skipped"
            assert result["items"][2]["state"] == "moved" and Path(result["items"][2]["destination"]).is_dir()
            assert wait_tidy(client, preview_id)["apply_job_id"] == job_id
            assert client.delete(f"/api/jobs/{preview_id}").status_code == 400
            assert client.delete(f"/api/jobs/{job_id}").status_code == 200
            assert client.delete(f"/api/jobs/{preview_id}").status_code == 200
            assert not tidy_plans.manifest(app.state.ctx.engine.strm, preview_id, lid).exists()
    finally:
        _cleanup()


@pytest.mark.parametrize("change", ["root", "mount", "retention", "manifest"])
def test_tidy_rejects_changed_preview_before_enqueue(tmp_path, change):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            root = tmp_path / "organized"
            lid = client.portal.call(library, app, root)
            old = scraped(root / "OLD-001", "OLD-001")
            age(root)
            pid = client.post(f"/api/libraries/{lid}/tidy?apply=false").json()["job_id"]
            plan = wait_tidy(client, pid)
            if change == "root":
                replacement = tmp_path / "replacement"
                replacement.mkdir()
                async def change_library():
                    await app.state.ctx.db._write("UPDATE libraries SET external_dir=? WHERE id=?", (str(replacement), lid))
                    await app.state.ctx.engine.reload_libraries()
                client.portal.call(change_library)
            elif change == "mount":
                root.rename(tmp_path / "original-mount")
                root.mkdir()
            elif change == "retention":
                client.portal.call(app.state.ctx.store.update, {"trash_days": 0})
            else:
                tidy_plans.manifest(app.state.ctx.engine.strm, pid, lid).unlink()
            response = client.post(f"/api/tidy/{pid}/apply", json={"preview_token": plan["preview_token"]})
            assert response.status_code == 409, response.text
            remaining = tmp_path / "original-mount" / "OLD-001" if change == "mount" else old
            assert remaining.is_dir()
    finally:
        _cleanup()


def test_old_tidy_jobs_are_not_reconstructed_from_current_files(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            async def seed():
                return await app.state.ctx.db.create_job("tidy", "legacy check", {"library_id": 1, "apply": False}, status="done")
            pid = client.portal.call(seed)
            old = client.get(f"/api/tidy/{pid}").json()
            assert old["legacy"] and not old["available"] and old["total"] == 0
            assert client.post(f"/api/tidy/{pid}/apply", json={"preview_token": "fake"}).status_code == 409
    finally:
        _cleanup()


def test_tidy_zero_retention_is_explicit_and_uses_original_scope(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            client.portal.call(app.state.ctx.store.update, {"trash_days": 0})
            root = tmp_path / "organized"
            lid = client.portal.call(library, app, root)
            directory = scraped(root / "OLD-001", "OLD-001")
            age(root)
            pid = client.post(f"/api/libraries/{lid}/tidy?apply=false").json()["job_id"]
            plan = wait_tidy(client, pid)
            assert directory.exists() and plan["libraries"][str(lid)]["days"] == 0
            job_id = client.post(f"/api/tidy/{pid}/apply", json={"preview_token": plan["preview_token"]}).json()["job_id"]
            result = wait_tidy(client, job_id)
            assert result["items"][0]["permanent"] and not result["items"][0]["destination"]
            assert not directory.exists() and not (root / ".hls2strm-trash").exists()
    finally:
        _cleanup()


def test_storage_original_files_survive_newer_preview_and_prefix_history_is_paged(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            base = "/api/libraries/1/storage"
            assert client.get(base + "/1/files").status_code == 401
            assert client.get("/api/strm/changes/1").status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})
            movie(app.state.ctx.engine.storage.root(1))
            def scan(keep):
                pid = client.post(base, json={"keep": keep}).json()["job_id"]
                for _ in range(300):
                    result = client.get(f"{base}/{pid}").json()
                    if result["status"] == "done":
                        return pid
                    time.sleep(.02)
                raise AssertionError("storage preview timeout")
            first, second = scan(0), scan(5)
            assert client.get(base).json()["job_id"] == second
            original = client.get(f"{base}/{first}/files?offset=2&limit=2").json()
            assert original["available"] and original["total"] == 5 and len(original["items"]) == 2
            assert client.get(f"{base}/{second}/files").json()["total"] == 0
            assert client.get(f"/api/libraries/999/storage/{first}/files").status_code == 404
            async def changes():
                db = app.state.ctx.db
                pid = await db.create_job("prefix", "prefix changes", {"old": "http://old", "new": "http://new"}, status="done")
                for n in range(3):
                    await db.add_strm_change(pid, f"/strm/{n}.strm", f"http://old/{n}", f"http://new/{n}")
                first_change = (await db.strm_changes(pid))[0]
                await db.mark_change_reverted(first_change["id"])
                return pid
            change_set = client.portal.call(changes)
            page = client.get(f"/api/strm/changes/{change_set}?offset=1&limit=1").json()
            assert page["total"] == 3 and len(page["items"]) == 1
            assert page["items"][0]["old"] == "http://old/1" and page["items"][0]["new"] == "http://new/1"
            assert client.get(f"/api/strm/changes/{change_set}").json()["items"][0]["reverted"] == 1
    finally:
        _cleanup()
