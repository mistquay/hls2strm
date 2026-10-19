import asyncio

import httpx
from fastapi.testclient import TestClient

from .test_auth import _client, _cleanup


def test_overview_isolates_and_caches_aggregate_but_refreshes_new_scans(tmp_path, monkeypatch):
    app = _client(tmp_path)
    try:
        with TestClient(app) as test_client:
            async def run():
                ctx = app.state.ctx
                ctx.engine.pause()
                db = ctx.db
                root = str(ctx.engine.maintenance.roots(ctx.engine.libs[1])[0])
                await db._write("INSERT OR REPLACE INTO maintenance_roots(library_id,root,scan_id,health) VALUES(1,?,1,'ok')", (root,))
                await db._write("INSERT INTO library_assets(scan_id,library_id,root,path,status,nfo_status,data,bytes) VALUES(1,1,?,'movie','present','valid','{}',100)", (root,))
                original = db._all
                entered, release = asyncio.Event(), asyncio.Event()
                aggregate_calls = 0

                async def observed(sql, *args, **kwargs):
                    nonlocal aggregate_calls
                    if "GROUP BY a.library_id,a.root,directory" in sql:
                        aggregate_calls += 1
                        assert kwargs.get("bulk") is True
                        async with db._read_locks["bulk"]:
                            entered.set()
                            await release.wait()
                    elif sql.startswith("SELECT * FROM maintenance_roots") or "kind LIKE 'maintenance_%'" in sql:
                        assert kwargs.get("browse") is True
                    return await original(sql, *args, **kwargs)

                monkeypatch.setattr(db, "_all", observed)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                             auth=("admin", "pw")) as client:
                    requests = []
                    try:
                        requests.append(asyncio.create_task(client.get("/api/maintenance/overview")))
                        await asyncio.wait_for(entered.wait(), 2)
                        requests.append(asyncio.create_task(client.get("/api/maintenance/overview")))
                        assert (await asyncio.wait_for(db._one("SELECT 1"), .5))[0] == 1
                        release.set()
                        results = await asyncio.gather(*requests)
                        assert all(response.status_code == 200 for response in results)
                        assert results[0].json()["groups"][0]["bytes"] == 100
                        assert aggregate_calls == 1

                        job = await db.create_job("maintenance_scan", "fresh progress", {}, status="paused")
                        response = await client.get("/api/maintenance/overview")
                        assert response.json()["jobs"][0]["id"] == job
                        assert aggregate_calls == 1

                        await db._write("INSERT INTO library_assets(scan_id,library_id,root,path,status,nfo_status,data,bytes) VALUES(2,1,?,'movie','present','valid','{}',200)", (root,))
                        await db._write("UPDATE maintenance_roots SET scan_id=2 WHERE library_id=1 AND root=?", (root,))
                        response = await client.get("/api/maintenance/overview")
                        assert response.json()["groups"][0]["bytes"] == 200
                        assert aggregate_calls == 2
                    finally:
                        release.set()
                        await asyncio.gather(*requests, return_exceptions=True)
            test_client.portal.call(run)
    finally:
        _cleanup()
