"""Installable PocketBase plugin; no core patches or import-time credentials."""
import json

from agent.secret_scope import get_secret
from .client import PocketBaseClient, PocketBaseError
from .service import PocketBaseService

ENV = ("POCKETBASE_HOST", "POCKETBASE_USERNAME", "POCKETBASE_PASSWORD")


def make_client():
    values = [get_secret(name) for name in ENV]
    if not all(values):
        raise ValueError("Set POCKETBASE_HOST, POCKETBASE_USERNAME and POCKETBASE_PASSWORD in the active profile")
    return PocketBaseClient(*values)


def check_requirements():
    try:
        make_client()
        return True
    except (ValueError, RuntimeError):
        return False


def handle(operation, args):
    try:
        result = operation(args)
    except (PocketBaseError, ValueError) as exc:
        result = {"success": False, "error": str(exc)}
    except (KeyError, TypeError, AttributeError):
        result = {"success": False, "error": "Invalid arguments or malformed PocketBase response"}
    return json.dumps(result)


def schema(name, actions, properties, description):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {"action": {"type": "string", "enum": actions}, **properties},
        "required": ["action"], "additionalProperties": False}}


COMMON = {
    "collection": {"type": "string", "description": "Exact collection name or ID; no system collections"},
    "filter": {"type": "string", "description": "PocketBase filter expression"},
    "page": {"type": "integer", "minimum": 1},
    "per_page": {"type": "integer", "minimum": 1, "maximum": 500},
    "sort": {"type": "string"},
    "data": {"type": "object", "description": "Record data or complete collection change set"},
    "snapshot_id": {"type": "string", "description": "Single-use preview ID; expires after five minutes"},
}
RECORDS_SCHEMA = schema("pocketbase_records", ["list", "get", "create", "update", "preview_delete", "delete"], {
    **COMMON, "record_id": {"type": "string"}, "fields": {"type": "string"},
    "date_field": {"type": "string", "description": "Required for bulk deletion"},
    "cutoff": {"type": "string", "description": "ISO date-time with UTC offset; exclusive less-than cutoff"},
    "timezone": {"type": "string", "description": "IANA timezone matching the cutoff offset"},
}, "List/search, get, create and update PocketBase records directly. Deletion requires preview_delete "
   "(one record_id, or date_field+cutoff+timezone with optional filter), then delete with ONLY action, "
   "collection and snapshot_id. The host asks one human approval for the frozen collection operation; "
   "no confirmed flag, blanket approvals or automatic retries. Never retry an uncertain write.")
COLLECTIONS_SCHEMA = schema("pocketbase_collections", ["list", "get", "create", "preview_update", "update"], COMMON,
    "Inspect/create PocketBase collections. Change fields, indexes and access rules with preview_update "
    "and a complete data change set, then update with ONLY action, collection and snapshot_id. "
    "All existing-schema changes require one human approval per reviewed collection change set. "
    "Omitted rules stay unchanged; null locks a rule; empty string makes it public. "
    "No collection deletion, rename or type conversion. Use full fields/indexes replacements.")


def register(ctx):
    service = PocketBaseService(make_client)
    ctx.register_hook("post_approval_response", service.approval.observe)
    for name, spec, operation in (("pocketbase_records", RECORDS_SCHEMA, service.records),
                                   ("pocketbase_collections", COLLECTIONS_SCHEMA, service.collections)):
        ctx.register_tool(name=name, toolset="pocketbase", schema=spec,
                          handler=lambda args, _operation=operation, **kwargs: handle(_operation, args),
                          check_fn=check_requirements, requires_env=list(ENV))
