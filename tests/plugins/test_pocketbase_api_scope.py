"""PocketBase previews across the real API-server run/context boundary."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.plugins.test_pocketbase import backend as _backend_fixture, SCHEMA  # shared loopback fixture
backend = _backend_fixture
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms import api_server_runs
from plugins.pocketbase.service import PocketBaseService
from plugins.pocketbase.client import PocketBaseClient
from tools import approval, approval_context


def run_turn(service, operation, args, *, run_id, session_id="conversation-a", principal="client-a", transport="api_server", notify=None):
    owner = SimpleNamespace(
        _profile_scope=lambda profile: nullcontext(),
        _bind_api_server_session=APIServerAdapter._bind_api_server_session,
        _memory_sessions=SimpleNamespace(checkin=lambda agent: None),
    )
    run = api_server_runs._RunLaunch(
        owner=owner, run_id=run_id, queue=api_server_runs._RunStream(), session_id=session_id,
        gateway_session_key=None, declared_selected=False, user_message="PocketBase operation",
        conversation_history=[], session_history_delivery=True,
        agent_kwargs={"room_dispatch": None}, request_profile=None,
        browser_control_principal=principal, browser_control_transport_family=transport,
    )
    agent = SimpleNamespace(run_conversation=lambda **kwargs: operation(args))
    api = SimpleNamespace(_publish_turn_process_ownership=lambda *args: None,
                          _clear_turn_process_ownership=lambda *args: None)
    return api_server_runs._run_agent_sync(owner, run, agent, notify or (lambda payload: None), _api_server=api)[0]


@pytest.mark.parametrize("operation", ["delete", "update"])
def test_preview_survives_two_api_runs_but_requires_second_run_human_approval(backend, monkeypatch, operation):
    from hermes_cli import lifecycle
    c = PocketBaseClient(backend["host"], "operator@example.invalid", "test-secret")
    service = PocketBaseService(lambda: c)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "")
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw:
                        service.approval.observe(**kw) if name == "post_approval_response" else None)
    prompts = []
    def approve(payload):
        prompts.append((approval_context.get_current_session_key(), payload))
        assert approval.resolve_gateway_approval("run-execute", "once", request_id=payload["request_id"]) == 1
    operation_fn = service.records if operation == "delete" else service.collections
    preview_args = {"action": "preview_delete", "collection": "notes", "record_id": "r1"} if operation == "delete" else {
        "action": "preview_update", "collection": "notes", "data": {"listRule": ""}}
    backend["replies"] = [(200, {"token": "private-token"}), (200, SCHEMA)]
    if operation == "delete":
        backend["replies"] += [(200, {"id": "r1"})]
    backend["replies"] += [(200, SCHEMA), (204, {}) if operation == "delete" else (200, {**SCHEMA, "listRule": ""})]
    preview = run_turn(service, operation_fn, preview_args, run_id="run-preview")["data"]
    assert prompts == []
    result = run_turn(service, operation_fn, {"action": operation, "collection": "notes",
                      "snapshot_id": preview["snapshot_id"]}, run_id="run-execute", notify=approve)
    assert result["success"] is True
    assert len(prompts) == 1
    assert prompts[0][0] == "run-execute"
    expected = ("deleted", ["r1"]) if operation == "delete" else ("listRule", "")
    assert result["data"][expected[0]] == expected[1]


@pytest.mark.parametrize("owner", [{"session_id": "conversation-b"}, {"principal": "client-b"}, {"transport": "other"}])
def test_api_preview_rejects_changed_authenticated_conversation(owner):
    c = PocketBaseClient("https://example.invalid", "test-user", "test-password")
    service = PocketBaseService(lambda: c)
    preview = run_turn(service, lambda args: service._save(c, args),
                       {"operation": "delete", "collection": "notes", "ids": ["r1"]}, run_id="preview")
    with pytest.raises(ValueError, match="changed operation scope"):
        run_turn(service, lambda args: service._take(c, args, "delete"),
                 {"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]},
                 run_id="execute", **owner)


def test_unbound_api_conversation_cannot_share_previews_between_runs():
    c = PocketBaseClient("https://example.invalid", "test-user", "test-password")
    service = PocketBaseService(lambda: c)
    preview = run_turn(service, lambda args: service._save(c, args),
                       {"operation": "delete", "collection": "notes", "ids": ["r1"]},
                       run_id="preview", session_id="")
    with pytest.raises(ValueError, match="changed operation scope"):
        run_turn(service, lambda args: service._take(c, args, "delete"),
                 {"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]},
                 run_id="execute", session_id="")


def test_api_preview_rejects_changed_profile_home(monkeypatch, tmp_path):
    c = PocketBaseClient("https://example.invalid", "test-user", "test-password")
    service = PocketBaseService(lambda: c)
    preview = run_turn(service, lambda args: service._save(c, args),
                       {"operation": "delete", "collection": "notes", "ids": ["r1"]}, run_id="preview")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
    with pytest.raises(ValueError, match="changed operation scope"):
        run_turn(service, lambda args: service._take(c, args, "delete"),
                 {"action": "delete", "collection": "notes", "snapshot_id": preview["snapshot_id"]}, run_id="execute")
