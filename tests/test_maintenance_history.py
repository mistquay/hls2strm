import asyncio
import json

from fastapi.testclient import TestClient

from hls2strm.maintenance_plan import CleanupRequest
from hls2strm.maintenance_query import AssetFilter
from .test_auth import _client, _cleanup
from .test_maintenance import fixture


def test_same_scope_active_unavailable_previews_are_reused(make_store, boot, tmp_path):
    async def run():
        e, db, lid, _, _ = await fixture(make_store, boot, tmp_path)
        try:
            request = CleanupRequest(action="unavailable", disposition="archive", query=AssetFilter(library_ids=[lid]))
            params = {"request": request.model_dump()}
            ids = await asyncio.gather(*(e.maintenance.create("maintenance_preview", params) for _ in range(8)))
            assert len(set(ids)) == 1
            first = ids[0]
            assert (await db.task_counts(first))["pending"] == 1
            await db.update_job(first, status="paused")
            equivalent = {"request": {**request.model_dump(), "query": {"library_ids": [lid, lid], "video_ids": []}}}
            assert await e.maintenance.create("maintenance_preview", equivalent) == first
            different = CleanupRequest(action="unavailable", disposition="archive", query=AssetFilter(library_ids=[lid], video_ids=[1]))
            assert await e.maintenance.create("maintenance_preview", {"request": different.model_dump()}) != first
            await db.update_job(first, status="done")
            assert await e.maintenance.create("maintenance_preview", params) != first
        finally:
            await db.close()
    asyncio.run(run())


def test_paginated_history_recovers_old_previews_and_related_jobs_after_restart(tmp_path):
    try:
        with TestClient(_client(tmp_path)) as client:
            assert client.get("/api/maintenance/jobs").status_code == 401
            client.post("/api/login", json={"username": "admin", "password": "pw"})

            async def seed():
                e = client.app.state.ctx.engine
                e.pause()
                request = CleanupRequest(action="unavailable", disposition="archive").model_dump()
                pid = await e.db.create_job("maintenance_preview", "old preview", {"request": request}, status="done")
                await e.db.update_job(pid, state={"ready": True, "eligible": 1, "preview_token": "stored-preview-token"})
                data = {"code": "TEST-001", "slug": "test-001", "title": "persisted result", "assets": [], "metadata": {}, "field_sources": {}}
                await e.db._write("INSERT INTO maintenance_items(preview_id,video_id,data) VALUES(?,1,?)", (pid, json.dumps(data)))
                apply = await e.db.create_job("maintenance_apply", "archived", {"preview_id": pid}, status="done")
                restore_preview = await e.db.create_job("maintenance_restore_preview", "restore preview", {"restore_of": apply}, status="done")
                restore = await e.db.create_job("maintenance_restore", "restored", {"preview_id": restore_preview}, status="done")
                for n in range(45):
                    await e.db.create_job("maintenance_scan", f"newer scan {n}", {"library_ids": [1]}, status="done")
                active = await e.db.create_job("maintenance_preview", "paused preview", {"request": request}, status="paused")
                await e.db.create_job("maintenance_preview", "resource preview", {"request": CleanupRequest().model_dump()}, status="done")
                await e.db.create_job("preview", "unrelated operation", {}, status="done")
                return pid, apply, restore_preview, restore, active

            ids = client.portal.call(seed)
            unavailable = client.get("/api/maintenance/jobs?category=unavailable").json()
            assert unavailable["total"] == 5
            assert {j["id"] for j in unavailable["items"]} == set(ids)
            assert all(j["operation_request"]["action"] == "unavailable" for j in unavailable["items"])
            first = client.get("/api/maintenance/jobs?limit=10").json()
            second = client.get("/api/maintenance/jobs?limit=10&offset=10").json()
            assert first["total"] == second["total"] == 51
            assert len(first["items"]) == len(second["items"]) == 10
            assert not {j["id"] for j in first["items"]} & {j["id"] for j in second["items"]}
            active = client.get("/api/maintenance/jobs?category=unavailable&status=active").json()
            assert [j["id"] for j in active["items"]] == [ids[-1]]
            done = client.get("/api/maintenance/jobs?category=unavailable&status=done").json()
            assert done["total"] == 4
            assert client.get("/api/maintenance/jobs?limit=101").status_code == 422
            assert client.get("/api/maintenance/jobs?category=invalid").status_code == 422

        # A new process/browser can reopen the original manifest; no new preview is required.
        with TestClient(_client(tmp_path)) as reopened:
            reopened.post("/api/login", json={"username": "admin", "password": "pw"})
            listing = reopened.get("/api/maintenance/jobs?category=unavailable&status=done").json()
            assert ids[0] in {j["id"] for j in listing["items"]}
            original = reopened.get(f"/api/maintenance/jobs/{ids[0]}").json()
            assert original["job"]["state"]["preview_token"] == "stored-preview-token"
            assert original["total"] == 1 and original["items"][0]["data"]["title"] == "persisted result"
    finally:
        _cleanup()
