"""PocketBase behavior contracts against an isolated local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def backend():
    state = {"requests": [], "replies": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"].append((self.command, self.path, dict(self.headers), json.loads(body) if body else None))
            from urllib.parse import parse_qs, urlsplit
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if (self.command == "GET" and parsed.path == "/api/collections"
                    and query.get("perPage") == ["500"] and query.get("sort") == ["id"]):
                page = int(query["page"][0])
                pages = state.get("schema_pages", [[SCHEMA]])
                status, payload = 200, {"items": pages[page - 1], "page": page,
                                        "totalPages": len(pages), "totalItems": sum(map(len, pages))}
            else:
                status, payload = state["replies"].pop(0) if state["replies"] else (200, {"token": "private-token"})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        do_GET = do_POST = do_PATCH = do_DELETE = do_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["host"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def client(backend):
    from plugins.pocketbase.client import PocketBaseClient
    return PocketBaseClient(backend["host"], "operator@example.invalid", "test-secret")


@pytest.mark.parametrize("stage,self_relation", [("preview", False), ("preview", True), ("approval", False), ("approval", True), ("digest", False)])
def test_inbound_cascade_graph_is_frozen_and_rejected(backend, stage, self_relation):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA),
                          (200, {"id": "r1"}), (200, SCHEMA), (204, {})]
    relation = {"id": "colnotes" if self_relation else "colother", "name": "notes" if self_relation else "other",
                "fields": [{"name": "parent", "type": "relation", "collectionId": "colnotes", "cascadeDelete": True}]}
    backend["schema_pages"] = [[SCHEMA], [relation]]
    service, prompts = deleting_service(backend)
    if stage == "preview":
        with pytest.raises(ValueError, match="cascade"):
            service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})
        assert prompts == []
        assert service._snapshots == {}
    else:
        backend["schema_pages"] = [[SCHEMA], [{"id": "colother", "name": "other", "fields": []}]]
        preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
        if stage == "digest":
            backend["schema_pages"][1][0]["name"] = "renamed"
        else:
            backend["schema_pages"][1] = [relation]
        with pytest.raises(ValueError, match="cascade|graph changed"):
            service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
        assert len(prompts) == 1
    assert not any(r[0] == "DELETE" for r in backend["requests"])
    assert any("page=2" in r[1] for r in backend["requests"])


@pytest.mark.parametrize("stage", ["patch_verification", "delete_verification", "delete_loop"])
def test_expiry_after_io_stops_frozen_writes(backend, stage, monkeypatch):
    now = [0]
    service, prompts = deleting_service(backend, clock=lambda: now[0])
    updating = stage == "patch_verification"
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA)]
    if updating:
        preview = service.collections({"action": "preview_update", "collection": "notes", "data": {"listRule": ""}})["data"]
        backend["replies"] = [(200, SCHEMA), (200, SCHEMA)]
    else:
        backend["replies"] += [(200, {"items": [{"id": f"r{i}"} for i in range(4)], "totalPages": 1, "totalItems": 4})]
        preview = service.records(preview_args())["data"]
        backend["replies"] = [(200, SCHEMA), (204, {}), (400, {}), (204, {}), (204, {})]
    c = service.client_factory()
    original = c.request
    def delayed(method, path, *args, **kwargs):
        try:
            return original(method, path, *args, **kwargs)
        finally:
            if ((stage == "patch_verification" and path == "/api/collections/colnotes")
                    or (stage == "delete_verification" and path == "/api/collections")
                    or (stage == "delete_loop" and path.endswith("/r1") and method == "DELETE")):
                now[0] = 301
    monkeypatch.setattr(c, "request", delayed)
    args = {"action": "update" if updating else "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]}
    if updating:
        with pytest.raises(ValueError, match="expired"):
            service.collections(args)
        assert not any(r[0] == "PATCH" for r in backend["requests"])
    else:
        result = service.records(args)["data"]
        assert result["expired"] is True
        assert result["deleted"] == (["r0"] if stage == "delete_loop" else [])
        assert [item["id"] for item in result["failed"]] == (["r1"] if stage == "delete_loop" else [])
        assert result["unattempted"] == (["r2", "r3"] if stage == "delete_loop" else ["r0", "r1", "r2", "r3"])
        assert len([r for r in backend["requests"] if r[0] == "DELETE"]) == (2 if stage == "delete_loop" else 0)
    assert len(prompts) == 1


@pytest.mark.parametrize("changed", ["totalItems", "totalPages"])
def test_bulk_pagination_totals_cannot_change(backend, changed):
    first = {"items": [{"id": "r1"}], "totalPages": 2, "totalItems": 3}
    second = {"items": [{"id": "r2"}], "totalPages": 2, "totalItems": 2}
    if changed == "totalPages":
        first["totalItems"] = 2
        second["totalPages"] = 1
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, first), (200, second)]
    service, prompts = deleting_service(backend)
    with pytest.raises(ValueError, match="pagination"):
        service.records(preview_args())
    assert service._snapshots == {}
    assert prompts == []


@pytest.mark.parametrize("method,status", [("DELETE", 500), ("PATCH", 502), ("POST", 504), ("GET", 500), ("DELETE", 400)])
def test_http_write_errors_are_method_aware_and_uncertain(backend, method, status):
    from plugins.pocketbase.client import PocketBaseError
    backend["replies"] = [(200, {"token": "private-token"}), (status, {"message": "test-secret"})]
    with pytest.raises(PocketBaseError) as error:
        client(backend).request(method, "/api/collections/notes/records/r1")
    uncertain = method != "GET" and status >= 500
    assert error.value.uncertain is uncertain
    assert ("inspect the exact target" in str(error.value)) is uncertain
    assert ("never automatically retry" in str(error.value)) is uncertain
    assert "test-secret" not in str(error.value)
    assert len(backend["requests"]) == 2


def test_auth_then_filtered_paginated_list(backend):
    backend["replies"] = [(200, {"token": "private-token"}), (200, {"items": [{"id": "a"}], "totalPages": 1})]
    result = client(backend).records("notes", filter='title = "hello"', page=2, per_page=10, sort="-created", fields="id,title")
    assert result["items"] == [{"id": "a"}]
    auth, listing = backend["requests"]
    assert auth[1] == "/api/collections/_superusers/auth-with-password"
    assert auth[3] == {"identity": "operator@example.invalid", "password": "test-secret"}
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(listing[1]).query) == {"filter": ['title = "hello"'], "page": ["2"], "perPage": ["10"], "sort": ["-created"], "fields": ["id,title"]}
    assert listing[2]["Authorization"] == "private-token"


@pytest.mark.parametrize("collection", ["../_superusers", "x/y", "notes?foo=1", "", "_superusers"])
def test_rejects_unsafe_record_collection_before_network(backend, collection):
    with pytest.raises(ValueError):
        client(backend).records(collection)
    assert backend["requests"] == []


@pytest.mark.parametrize("page,per_page", [(0, 10), (1, 0), (1, 501), (True, 10)])
def test_rejects_invalid_pagination(backend, page, per_page):
    with pytest.raises(ValueError):
        client(backend).records("notes", page=page, per_page=per_page)
    assert backend["requests"] == []


@pytest.mark.parametrize("status", [400, 401, 403, 500, 302])
def test_sanitized_errors_never_retry_writes(backend, status):
    from plugins.pocketbase.client import PocketBaseError
    backend["replies"] = [(200, {"token": "private-token"}), (status, {"message": "private-token test-secret", "data": {"password": "leak"}})]
    with pytest.raises(PocketBaseError) as exc:
        client(backend).request("POST", "/api/collections/notes/records", {"title": "hello"})
    assert "test-secret" not in str(exc.value)
    assert "private-token" not in str(exc.value)
    assert "leak" not in str(exc.value)
    assert exc.value.status == status
    assert len(backend["requests"]) == 2


@pytest.mark.parametrize("host", ["ftp://example.com", "https://u:p@example.com", "https://example.com/?x=1", "http://example.com"])
def test_rejects_unsafe_host(host):
    from plugins.pocketbase.client import PocketBaseClient
    with pytest.raises(ValueError):
        PocketBaseClient(host, "u", "p")


@pytest.mark.parametrize("action,method,suffix,data", [
    ("get", "GET", "/r1", None),
    ("create", "POST", "", {"title": "hello"}),
    ("update", "PATCH", "/r1", {"title": "changed"}),
])
def test_ordinary_record_operations_are_direct_and_sanitized(backend, action, method, suffix, data):
    from plugins.pocketbase.service import PocketBaseService
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1", "title": "hello", "password": "hidden", "nested": {"token": "hidden"}, "note": "private-token"})]
    service = PocketBaseService(lambda: client(backend))
    result = service.records({"action": action, "collection": "notes", "record_id": "r1", "data": data})
    assert result == {"success": True, "data": {"id": "r1", "title": "hello", "password": "[REDACTED]", "nested": {"token": "[REDACTED]"}, "note": "[REDACTED]"}}
    assert backend["requests"][-1][0:2] == (method, "/api/collections/notes/records" + suffix)
    assert backend["requests"][-1][3] == data


@pytest.mark.parametrize("choice,allowed", [("once", True), ("session", True), ("always", True), ("deny", False), ("timeout", False), ("cancelled", False), (None, False)])
def test_destructive_gate_requires_actual_human_receipt(choice, allowed):
    from plugins.pocketbase.approval import HumanApproval
    calls = []
    def request(tool, reason, *, rule_key):
        calls.append(rule_key)
        if choice:
            gate.observe(pattern_key="plugin_rule:" + rule_key, choice=choice)
        return {"approved": choice not in {"deny", "timeout", "cancelled"}}
    gate = HumanApproval(request)
    assert gate.confirm("pocketbase_records", "Delete exactly two notes") is allowed
    assert gate.confirm("pocketbase_records", "Delete exactly two notes") is allowed
    assert calls[0] != calls[1]


SCHEMA = {"id": "colnotes", "name": "notes", "type": "base", "fields": [{"id": "datefield", "name": "created", "type": "autodate"}]}


def deleting_service(backend, choice="once", clock=None):
    from plugins.pocketbase.service import PocketBaseService
    from plugins.pocketbase.approval import HumanApproval
    prompts = []
    def request(tool, reason, *, rule_key):
        prompts.append(reason)
        if choice:
            gate.observe(pattern_key="plugin_rule:" + rule_key, choice=choice)
        return {"approved": choice in {"once", "session", "always"}}
    gate = HumanApproval(request)
    c = client(backend)
    return PocketBaseService(lambda: c, approval=gate, clock=clock), prompts


def preview_args():
    return {"action": "preview_delete", "collection": "notes", "date_field": "created", "cutoff": "2026-10-01T00:00:00+02:00", "timezone": "Europe/Berlin", "filter": 'title != "keep"'}


def test_bulk_delete_freezes_all_pages_one_approval_and_reports_failures(backend):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA),
                          (200, {"items": [{"id": "r1"}], "totalPages": 2, "totalItems": 2}),
                          (200, {"items": [{"id": "r2"}], "totalPages": 2, "totalItems": 2}),
                          (200, SCHEMA), (204, {}), (500, {"message": "private-token"})]
    service, prompts = deleting_service(backend)
    preview = service.records(preview_args())["data"]
    assert preview["count"] == 2
    assert preview["ids"] == ["r1", "r2"]
    assert preview["cutoff_utc"] == "2026-09-30 22:00:00.000Z"
    result = service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert result["data"]["deleted"] == ["r1"]
    assert result["data"]["failed"] == [{"id": "r2", "error": "PocketBase request failed (HTTP 500); write outcome may be uncertain; inspect the exact target and never automatically retry", "uncertain": True}]
    assert len(prompts) == 1
    assert all(part in prompts[0] for part in ["notes", "2", "created", "2026-10-01T00:00:00+02:00", "Europe/Berlin"])
    assert [r[1] for r in backend["requests"] if r[0] == "DELETE"] == ["/api/collections/colnotes/records/r1", "/api/collections/colnotes/records/r2"]
    with pytest.raises(ValueError):
        service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert len(prompts) == 1


@pytest.mark.parametrize("token", [None, "", 42])
def test_invalid_auth_token_is_sanitized(backend, token):
    from plugins.pocketbase.client import PocketBaseError
    backend["replies"] = [(200, {"token": token})]
    with pytest.raises(PocketBaseError):
        client(backend).records("notes")
    assert len(backend["requests"]) == 1


def test_snapshot_capacity_and_expired_cleanup(backend):
    service, _ = deleting_service(backend, clock=lambda: 0)
    c = client(backend)
    plan = {"operation": "delete", "collection": "notes", "ids": []}
    for _ in range(100):
        service._save(c, plan)
    with pytest.raises(ValueError, match="capacity"):
        service._save(c, plan)
    service.clock = lambda: 301
    service._save(c, plan)
    assert len(service._snapshots) == 1


@pytest.mark.parametrize("action,method,path,data", [
    ("list", "GET", "/api/collections?page=1&perPage=100", None),
    ("get", "GET", "/api/collections/notes", None),
    ("create", "POST", "/api/collections", {"name": "notes", "type": "base", "fields": [{"name": "title", "type": "text"}], "indexes": [], "listRule": None}),
])
def test_collection_inspect_and_create(backend, action, method, path, data):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA)]
    service, prompts = deleting_service(backend)
    result = service.collections({"action": action, "collection": "notes", "data": data})
    assert result["success"] is True
    assert result["data"]["id"] == "colnotes"
    assert backend["requests"][-1][:2] == (method, path)
    assert backend["requests"][-1][3] == data
    assert prompts == []


def test_schema_change_set_is_frozen_and_approved_once_per_collection(backend):
    changes = {"fields": [{"id": "datefield", "name": "created", "type": "autodate"}, {"name": "title", "type": "text"}], "indexes": ["CREATE INDEX idx_title ON notes (title)"], "listRule": "", "deleteRule": None}
    updated = {**SCHEMA, **changes}
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, SCHEMA), (200, updated)]
    service, prompts = deleting_service(backend)
    preview = service.collections({"action": "preview_update", "collection": "notes", "data": changes})["data"]
    changes["listRule"] = "malicious edit after preview"
    result = service.collections({"action": "update", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert result["data"]["listRule"] == ""
    assert backend["requests"][-1][:2] == ("PATCH", "/api/collections/colnotes")
    assert backend["requests"][-1][3]["listRule"] == ""
    assert len(prompts) == 1
    assert all(part in prompts[0] for part in ["notes", "fields", "indexes", "listRule", "deleteRule"])


@pytest.mark.parametrize("change", ["collection", "selection", "expiry", "session", "host", "credentials"])
def test_snapshot_scope_changes_block_without_prompt_or_write(backend, monkeypatch, change):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"})]
    service, prompts = deleting_service(backend, clock=lambda: 0)
    preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
    args = {"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]}
    changes = {
        "collection": lambda: args.update(collection="other"),
        "selection": lambda: args.update(filter=""),
        "expiry": lambda: setattr(service, "clock", lambda: 301),
        "session": lambda: monkeypatch.setattr("tools.approval_context.get_current_session_key", lambda: "other-session"),
        "host": lambda: setattr(service.client_factory(), "host", service.client_factory().host + "/different"),
        "credentials": lambda: setattr(service.client_factory(), "password", "changed"),
    }
    changes[change]()
    with pytest.raises(ValueError):
        service.records(args)
    assert prompts == []
    assert not any(r[0] == "DELETE" for r in backend["requests"])


@pytest.mark.parametrize("choice", ["deny", "timeout", None])
def test_delete_denial_no_writes(backend, choice):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"})]
    service, prompts = deleting_service(backend, choice=choice)
    preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
    result = service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert result["success"] is False
    assert result["data"]["approved"] is False
    assert len(prompts) == 1
    assert not any(r[0] == "DELETE" for r in backend["requests"])


def test_collection_change_during_approval_invalidates_snapshot(backend):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"}), (200, {**SCHEMA, "name": "renamed"})]
    service, prompts = deleting_service(backend)
    preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
    with pytest.raises(ValueError, match="Collection changed"):
        service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert len(prompts) == 1
    assert not any(r[0] == "DELETE" for r in backend["requests"])


@pytest.mark.parametrize("data", [{}, None])
def test_record_write_requires_json_object(backend, data):
    service, _ = deleting_service(backend)
    with pytest.raises(ValueError):
        service.records({"action": "create", "collection": "notes", "data": data})
    assert backend["requests"] == []


def test_get_requires_record_id(backend):
    service, _ = deleting_service(backend)
    with pytest.raises(ValueError):
        service.records({"action": "get", "collection": "notes"})
    assert backend["requests"] == []


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch, backend):
    from pathlib import Path
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [pocketbase]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: Path(__file__).resolve().parents[2] / "plugins")
    token = set_secret_scope({"POCKETBASE_HOST": backend["host"], "POCKETBASE_USERNAME": "operator@example.invalid", "POCKETBASE_PASSWORD": "test-secret"})
    manager = PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    manager.discover_and_load()
    yield manager
    manager.unload()
    reset_secret_scope(token)


def test_real_discovery_registration_and_structured_error(backend, loaded_plugin):
    from tools.registry import registry
    manager = loaded_plugin
    assert manager._plugins["pocketbase"].error is None
    entry = registry.get_entry("pocketbase_records", scope=manager.scope_key)
    assert entry.check_fn() is True
    assert json.loads(registry.dispatch("pocketbase_records", {"action": "delete", "collection": "notes", "confirmed": True}, scope=manager.scope_key))["success"] is False
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"items": []})]
    assert json.loads(registry.dispatch("pocketbase_records", {"action": "list", "collection": "notes"}, scope=manager.scope_key))["data"] == {"items": []}


def test_real_gateway_human_approval_deletes_snapshot_once(backend, loaded_plugin, monkeypatch):
    from tools.registry import registry
    from tools import approval, approval_context
    from hermes_cli import lifecycle
    manager = loaded_plugin
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    monkeypatch.setattr(lifecycle, "invoke_hook", manager.invoke_hook)
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"}),
                          (200, {"token": "private-token"}), (200, SCHEMA), (204, {})]
    preview = json.loads(registry.dispatch("pocketbase_records", {"action": "preview_delete", "collection": "notes", "record_id": "r1"}, scope=manager.scope_key))["data"]
    prompts = []
    session_key = approval_context.get_current_session_key()
    def approve(payload):
        prompts.append(payload)
        assert approval.resolve_gateway_approval(session_key, "once", request_id=payload["request_id"]) == 1
    approval.register_gateway_notify(session_key, approve)
    try:
        result = json.loads(registry.dispatch("pocketbase_records", {"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]}, scope=manager.scope_key))
    finally:
        approval.unregister_gateway_notify(session_key)
    assert result["data"]["deleted"] == ["r1"]
    assert len(prompts) == 1


@pytest.mark.parametrize("bad_page", [
    {"items": [{"id": "r1"}], "totalPages": 1, "totalItems": 2},
    {"items": [{"id": "r1"}, {"id": "r1"}], "totalPages": 1, "totalItems": 2},
    {"items": [], "totalPages": 1001, "totalItems": 500001},
])
def test_incomplete_duplicate_or_unbounded_selection_fails_closed(backend, bad_page):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, bad_page)]
    service, prompts = deleting_service(backend)
    with pytest.raises(ValueError):
        service.records(preview_args())
    assert prompts == []
    assert service._snapshots == {}


def test_preview_return_mutation_cannot_change_frozen_ids(backend):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"}), (200, SCHEMA), (204, {})]
    service, prompts = deleting_service(backend)
    preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
    preview["ids"].append("r2")
    result = service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert result["data"]["deleted"] == ["r1"]


def test_snapshot_expiry_during_approval_blocks_write(backend):
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA), (200, {"id": "r1"})]
    now = [0]
    service, _ = deleting_service(backend, clock=lambda: now[0])
    preview = service.records({"action": "preview_delete", "collection": "notes", "record_id": "r1"})["data"]
    def delayed_approval(tool, reason):
        now[0] = 301
        return True
    service.approval.confirm = delayed_approval
    with pytest.raises(ValueError, match="expired during approval"):
        service.records({"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]})
    assert not any(r[0] == "DELETE" for r in backend["requests"])


def test_missing_profile_secrets_do_not_fall_back_to_launch_environment(monkeypatch):
    from plugins.pocketbase import check_requirements, make_client
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    monkeypatch.setenv("POCKETBASE_HOST", "https://launch.invalid")
    monkeypatch.setenv("POCKETBASE_USERNAME", "launch")
    monkeypatch.setenv("POCKETBASE_PASSWORD", "launch")
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    multiplex = set_multiplex_context(True)
    token = set_secret_scope({})
    try:
        assert check_requirements() is False
        with pytest.raises(ValueError):
            make_client()
    finally:
        reset_secret_scope(token)
        reset_multiplex_context(multiplex)


@pytest.mark.parametrize("action", ["list", "get", "create", "update", "preview_delete"])
def test_system_collection_id_cannot_bypass_name_guard(backend, action):
    backend["replies"] = [(200, {"token": "private-token"}), (200, {"id": "pbc123", "name": "_superusers", "system": True, "type": "auth", "fields": []})]
    service, prompts = deleting_service(backend)
    with pytest.raises(ValueError, match="System collections"):
        service.records({"action": action, "collection": "pbc123", "record_id": "r1", "data": {"email": "x"}})
    assert not any(r[0] in {"PATCH", "DELETE"} or (r[0] == "POST" and r[1].endswith("/records")) for r in backend["requests"])
    assert prompts == []


@pytest.mark.parametrize("args,expected", [({}, {"page": ["1"], "perPage": ["100"]}),
    ({"page": 2}, {"page": ["2"], "perPage": ["100"]}),
    ({"per_page": 25}, {"page": ["1"], "perPage": ["25"]}),
    ({"page": 2, "per_page": 25}, {"page": ["2"], "perPage": ["25"]})])
def test_collection_list_sends_validated_pagination_defaults(backend, args, expected):
    from urllib.parse import parse_qs, urlsplit
    service, _ = deleting_service(backend)
    service.collections({"action": "list", **args})
    assert parse_qs(urlsplit(backend["requests"][-1][1]).query) == expected
