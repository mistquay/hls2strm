import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import FakeFetcher
from .test_auth import _client, _cleanup
from .test_engine import build


@pytest.mark.parametrize("kind", ["same", "nested", "external"])
def test_output_root_rejects_library_collisions_atomically(tmp_path, kind):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.auth = ("admin", "pw")
            engine = app.state.ctx.engine
            new_output = tmp_path / "new-output"
            collision = new_output / engine.libs[1]["dir"]
            directory = collision / "child" if kind == "nested" else collision
            body = {"name": "absolute", "dir": str(directory)}
            if kind == "external":
                body.update(dir=str(tmp_path / "inbox"), external_dir=str(collision))
            assert client.post("/api/libraries", json=body).status_code == 200
            before = client.get("/api/settings").json()
            saved = client.portal.call(app.state.ctx.db.get_setting, "settings")
            response = client.put("/api/settings", json={"output_dir": str(new_output)})
            assert response.status_code == 422
            assert "output_dir" in response.json()["detail"]
            assert client.get("/api/settings").json() == before
            assert client.portal.call(app.state.ctx.db.get_setting, "settings") == saved
    finally:
        _cleanup()


def test_path_settings_validate_final_combined_configuration(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.auth = ("admin", "pw")
            old_archive = app.state.ctx.store.archive_dir
            new_output = old_archive / "new-output"
            new_archive = tmp_path / "new-archive"
            response = client.put("/api/settings", json={
                "output_dir": str(new_output), "archive_dir": str(new_archive)})
            assert response.status_code == 200
            assert Path(response.json()["effective"]["output_dir"]) == new_output
            before = client.get("/api/settings").json()
            invalid = client.put("/api/settings", json={
                "output_dir": str(tmp_path / "other-output"),
                "archive_dir": str(tmp_path / "other-output" / "archive")})
            assert invalid.status_code == 422
            assert client.get("/api/settings").json() == before
    finally:
        _cleanup()


def test_output_root_checks_archive_and_environment_fallback(tmp_path):
    app = _client(tmp_path)
    try:
        with TestClient(app) as client:
            client.auth = ("admin", "pw")
            before = client.get("/api/settings").json()
            archive = app.state.ctx.store.archive_dir
            assert client.put("/api/settings", json={"output_dir": str(archive / "output")}).status_code == 422
            assert client.get("/api/settings").json() == before

            engine = app.state.ctx.engine
            original_output = engine.store.output_dir
            assert client.put("/api/settings", json={"output_dir": str(tmp_path / "moved")}).status_code == 200
            body = {"name": "absolute", "dir": str(original_output / engine.libs[1]["dir"])}
            assert client.post("/api/libraries", json=body).status_code == 200
            assert client.put("/api/settings", json={"output_dir": ""}).status_code == 422
    finally:
        _cleanup()


def test_path_settings_wait_for_active_output_writes(make_store, boot, tmp_path):
    async def run():
        db, store = await make_store()
        engine = build(db, store, boot, FakeFetcher("", {}))
        await engine.reload_libraries()
        before = store.output_dir
        new_root = tmp_path / "changed"
        change = None
        try:
            async with engine.file_changes.normal():
                change = asyncio.create_task(engine.update_settings({"output_dir": str(new_root)}))
                await asyncio.sleep(.02)
                assert not change.done() and store.output_dir == before
            await asyncio.wait_for(change, 2)
            assert store.output_dir == new_root
            assert (await db.get_setting("output_sync")) is not None
        finally:
            if change is not None and not change.done():
                change.cancel()
                await asyncio.gather(change, return_exceptions=True)
    asyncio.run(run())
