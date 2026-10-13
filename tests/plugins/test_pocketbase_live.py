"""Optional real PocketBase integration; only a disposable loopback server is used.

Put a PocketBase binary on PATH to exercise this test. No production environment
variables are read and no external PocketBase host is contacted.
"""
import json
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from plugins.pocketbase.approval import HumanApproval
from plugins.pocketbase.client import PocketBaseClient
from plugins.pocketbase.service import PocketBaseService


@pytest.fixture
def pocketbase(tmp_path):
    binary = shutil.which("pocketbase")
    if not binary:
        pytest.skip("Put a PocketBase binary on PATH for the disposable integration test")
    directory = str(tmp_path / "pb_data")
    subprocess.run([binary, "superuser", "upsert", "test@example.invalid", "local-only-test-password",
                    "--dir", directory], check=True, capture_output=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    host = f"http://127.0.0.1:{port}"
    process = subprocess.Popen([binary, "serve", "--http", f"127.0.0.1:{port}", "--dir", directory],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(host + "/api/health", timeout=1) as response:
                    assert response.status == 200
                break
            except URLError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Disposable PocketBase did not become healthy")
                time.sleep(0.1)
        yield PocketBaseClient(host, "test@example.invalid", "local-only-test-password")
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_real_pocketbase_record_and_schema_roundtrip(pocketbase):
    prompts = []
    def request(tool, reason, *, rule_key):
        prompts.append(reason)
        gate.observe(pattern_key="plugin_rule:" + rule_key, choice="once")
        return {"approved": True}
    gate = HumanApproval(request)
    service = PocketBaseService(lambda: pocketbase, approval=gate)
    collection = service.collections({"action": "create", "data": {
        "name": "notes", "type": "base", "fields": [{"name": "title", "type": "text"}, {"name": "observed", "type": "date"}]}})["data"]
    assert collection["name"] == "notes"
    created = [service.records({"action": "create", "collection": "notes", "data": {
        "title": title, "observed": "2026-01-01 00:00:00.000Z"}})["data"] for title in ("first", "second")]
    listed = service.records({"action": "list", "collection": "notes", "filter": 'title != "missing"', "page": 2, "per_page": 1, "sort": "title"})["data"]
    assert listed["totalItems"] == 2
    assert len(listed["items"]) == 1
    service.records({"action": "update", "collection": "notes", "record_id": created[0]["id"], "data": {"title": "changed"}})
    assert service.records({"action": "get", "collection": "notes", "record_id": created[0]["id"]})["data"]["title"] == "changed"
    preview = service.collections({"action": "preview_update", "collection": "notes", "data": {"listRule": "", "indexes": ["CREATE INDEX idx_title ON notes (title)"]}})["data"]
    service.collections({"action": "update", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert service.collections({"action": "get", "collection": "notes"})["data"]["listRule"] == ""
    preview = service.records({"action": "preview_delete", "collection": "notes", "date_field": "observed", "cutoff": "2026-10-01T00:00:00+02:00", "timezone": "Europe/Berlin"})["data"]
    assert preview["count"] == 2
    result = service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert sorted(result["data"]["deleted"]) == sorted(r["id"] for r in created)
    assert result["data"]["failed"] == []
    assert service.records({"action": "list", "collection": "notes"})["data"]["totalItems"] == 0
    assert len(prompts) == 2  # one schema change set + one collection-wide deletion
