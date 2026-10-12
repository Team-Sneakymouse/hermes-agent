"""PocketBase operations, returning only sanitized structured results."""
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .client import PocketBaseError
from .approval import HumanApproval


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()



def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(word in key.lower() for word in
                ("password", "token", "secret", "authorization", "apikey", "api_key")) else
                sanitize(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


class PocketBaseService:
    def __init__(self, client_factory, approval=None, clock=None):
        self.client_factory = client_factory
        self.approval = approval or HumanApproval()
        self.clock = clock or time.monotonic
        self._snapshots = {}
        self._lock = threading.Lock()

    def _scope(self, client):
        from hermes_constants import get_hermes_home
        from tools.approval_context import get_current_session_key
        return (str(get_hermes_home()), get_current_session_key(), client.host,
                fingerprint([client.username, client.password]))

    def _save(self, client, plan):
        snapshot_id = uuid.uuid4().hex
        with self._lock:
            self._snapshots = {key: item for key, item in self._snapshots.items() if item[1] > self.clock()}
            if len(self._snapshots) >= 100:
                raise ValueError("Snapshot capacity reached; wait for expiry or consume previews")
            self._snapshots[snapshot_id] = (self._scope(client), self.clock() + 300,
                                            json.loads(json.dumps(plan)))
        return {**plan, "snapshot_id": snapshot_id, "expires_in_seconds": 300}

    def _take(self, client, args, operation):
        with self._lock:
            entry = self._snapshots.pop(args.get("snapshot_id"), None)
        if entry is None:
            raise ValueError("Unknown or already consumed snapshot; preview again")
        scope, expires, plan = entry
        if (scope != self._scope(client) or self.clock() >= expires
                or args.get("collection") != plan["collection"] or plan["operation"] != operation):
            raise ValueError("Expired or changed operation scope; preview again")
        if set(args) != {"action", "collection", "snapshot_id"}:
            raise ValueError("Selection changes require a new preview and approval")
        return {**plan, "_expires": expires}

    def _schema(self, client, collection):
        client.identifier(collection)
        schema = client.request("GET", "/api/collections/" + collection)
        if schema.get("system") or schema.get("name", "").startswith("_"):
            raise ValueError("System collections are not supported")
        return schema

    def _deletion_graph_digest(self, client, collection_id):
        schemas, page, totals = [], 1, None
        while True:
            response = client.request("GET", "/api/collections", params={
                "page": page, "perPage": 500, "sort": "id"})
            current = (response.get("totalPages"), response.get("totalItems"))
            if (any(type(value) is not int or value < 0 for value in current)
                    or response.get("page") != page or not isinstance(response.get("items"), list)):
                raise ValueError("Invalid collection graph pagination")
            if totals is None:
                totals = current
            if current != totals:
                raise ValueError("Collection graph changed during pagination")
            schemas.extend(response["items"])
            if page >= totals[0]:
                break
            page += 1
        if len(schemas) != totals[1]:
            raise ValueError("Incomplete collection graph")
        for schema in schemas:
            for field in schema["fields"]:
                if (field.get("type") == "relation" and field.get("collectionId") == collection_id
                        and field.get("cascadeDelete")):
                    raise ValueError("Incoming cascadeDelete relation; deletion is outside frozen scope")
        if len({schema["id"] for schema in schemas}) != len(schemas):
            raise ValueError("Duplicate collection graph entries")
        return fingerprint(sorted(schemas, key=lambda schema: schema["id"]))

    def _preview_delete(self, client, args):
        schema = self._schema(client, args["collection"])
        collection_id = client.identifier(schema["id"])
        graph_digest = self._deletion_graph_digest(client, collection_id)
        selection = {key: args.get(key) for key in ("filter", "date_field", "cutoff", "timezone")}
        if args.get("record_id"):
            record_id = client.identifier(args["record_id"])
            client.request("GET", client.record_path(collection_id, record_id))
            ids = [record_id]
            cutoff_utc = None
        else:
            field = client.identifier(args.get("date_field"))
            if not any(f["name"] == field and f["type"] in {"date", "autodate"} for f in schema["fields"]):
                raise ValueError("Bulk deletion requires a date field in the collection schema")
            zone = ZoneInfo(args["timezone"])
            cutoff = datetime.fromisoformat(args["cutoff"])
            if cutoff.tzinfo is None or cutoff.utcoffset() != cutoff.astimezone(zone).utcoffset():
                raise ValueError("Cutoff must include an offset matching the specified timezone")
            cutoff_utc = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
            filter_text = f'{field} < "{cutoff_utc}"'
            if args.get("filter"):
                filter_text = f'({args["filter"]}) && ({filter_text})'
            ids = []
            page = 1
            totals = None
            while True:
                response = client.records(collection_id, filter=filter_text, page=page,
                                          per_page=500, fields="id", sort="id")
                if (type(response.get("totalPages")) is not int or not 0 <= response["totalPages"] <= 20
                        or type(response.get("totalItems")) is not int or not 0 <= response["totalItems"] <= 10000):
                    raise ValueError("Bulk preview exceeds 10000 records or has invalid pagination metadata")
                current = (response["totalPages"], response["totalItems"])
                if totals is None:
                    totals = current
                if (current != totals or ("page" in response and response["page"] != page)
                        or len(response["items"]) > 500):
                    raise ValueError("Selection changed during pagination; preview again")
                ids.extend(client.identifier(item["id"]) for item in response["items"])
                if page >= response["totalPages"]:
                    break
                page += 1
            if len(ids) != response["totalItems"] or len(set(ids)) != len(ids):
                raise ValueError("Selection changed during pagination; preview again")
        return self._save(client, {"operation": "delete", "collection": args["collection"],
                                  "collection_id": collection_id, "schema_digest": fingerprint(schema),
                                  "graph_digest": graph_digest,
                                  "ids": ids, "count": len(ids), **selection, "cutoff_utc": cutoff_utc})

    def _delete(self, client, args):
        plan = self._take(client, args, "delete")
        reason = (f'Delete exactly {plan["count"]} records from {plan["collection"]} '
                  f'(id {plan["collection_id"]}); date field={plan["date_field"]}; '
                  f'cutoff={plan["cutoff"]}; timezone={plan["timezone"]}; '
                  f'filter={plan["filter"]}; frozen IDs={plan["ids"]}. One operation only.')
        if not self.approval.confirm("pocketbase_records", reason):
            return {"approved": False, "deleted": [], "failed": [], "message": "Human approval required; preview again"}
        if self.clock() >= plan["_expires"]:
            raise ValueError("Snapshot expired during approval; preview again")
        if fingerprint(self._schema(client, plan["collection_id"])) != plan["schema_digest"]:
            raise ValueError("Collection changed; preview and approve again")
        if self._deletion_graph_digest(client, plan["collection_id"]) != plan["graph_digest"]:
            raise ValueError("Collection graph changed; preview and approve again")
        deleted, failed = [], []
        for index, record_id in enumerate(plan["ids"]):
            if self.clock() >= plan["_expires"]:
                return {"approved": True, "deleted": deleted, "failed": failed,
                        "unattempted": plan["ids"][index:], "expired": True,
                        "message": "Snapshot expired; preview and approve again for unattempted IDs"}
            try:
                client.request("DELETE", client.record_path(plan["collection_id"], record_id))
                deleted.append(record_id)
            except PocketBaseError as exc:
                failed.append({"id": record_id, "error": str(exc), "uncertain": exc.uncertain})
        return {"approved": True, "deleted": deleted, "failed": failed}


    def _collection_list(self, client, args):
        page, per_page = args.get("page", 1), args.get("per_page", 100)
        if type(page) is not int or page < 1 or type(per_page) is not int or not 1 <= per_page <= 500:
            raise ValueError("Invalid collection pagination")
        return client.request("GET", "/api/collections", params={
            "page": args.get("page"), "perPage": args.get("per_page"),
            "filter": args.get("filter"), "sort": args.get("sort")})

    def _collection_create(self, client, args):
        data = self._validate_schema_data(client, args.get("data"), create=True)
        return client.request("POST", "/api/collections", data)

    def _preview_update(self, client, args):
        data = self._validate_schema_data(client, args.get("data"))
        schema = self._schema(client, args["collection"])
        return self._save(client, {"operation": "update", "collection": args["collection"],
                                   "collection_id": client.identifier(schema["id"]),
                                   "schema_digest": fingerprint(schema), "before": schema, "changes": data})

    def _update(self, client, args):
        plan = self._take(client, args, "update")
        reason = (f'Update exactly collection {plan["collection"]} (id {plan["collection_id"]}). '
                  f'Reviewed change set: {json.dumps(plan["changes"], sort_keys=True)}. '
                  f'Current schema: {json.dumps(plan["before"], sort_keys=True)}. One operation only.')
        if not self.approval.confirm("pocketbase_collections", sanitize(reason, (client.password, client.token))):
            return {"approved": False, "error": "Human approval required; preview again"}
        if self.clock() >= plan["_expires"]:
            raise ValueError("Snapshot expired during approval; preview again")
        if fingerprint(self._schema(client, plan["collection_id"])) != plan["schema_digest"]:
            raise ValueError("Collection changed; preview and approve again")
        if self.clock() >= plan["_expires"]:
            raise ValueError("Snapshot expired during verification; preview again")
        return client.request("PATCH", "/api/collections/" + plan["collection_id"], plan["changes"])

    def collections(self, args):
        client = self.client_factory()
        dispatch = {"list": self._collection_list, "get": lambda c, a: self._schema(c, a["collection"]),
                    "create": self._collection_create, "preview_update": self._preview_update,
                    "update": self._update}
        operation = dispatch.get(args.get("action"))
        if operation is None:
            raise ValueError("Unknown collection action; collection deletion is not supported")
        result = operation(client, args)
        if result.get("approved") is False:
            return {"success": False, "error": result["error"]}
        return {"success": True, "data": sanitize(result, (client.password, client.token))}

    @staticmethod
    def _validate_schema_data(client, data, create=False):
        allowed = {"fields", "indexes", "listRule", "viewRule", "createRule", "updateRule", "deleteRule"}
        if create:
            allowed |= {"name", "type"}
        if not isinstance(data, dict) or not data or set(data) - allowed:
            raise ValueError("Unsupported or empty collection change set")
        if create:
            client.identifier(data.get("name"))
            if data.get("type", "base") not in {"base", "auth"}:
                raise ValueError("Only base/auth collection creation is supported")
        return json.loads(json.dumps(data))

    def _record_direct(self, client, args):
        action = args["action"]
        if action in {"get", "update"} and not args.get("record_id"):
            raise ValueError("record_id is required")
        if action in {"create", "update"} and (not isinstance(args.get("data"), dict) or not args["data"]):
            raise ValueError("A nonempty record data object is required")
        self._schema(client, args["collection"])
        path = client.record_path(args["collection"], args.get("record_id") if action != "create" else None)
        method = {"get": "GET", "create": "POST", "update": "PATCH"}[action]
        return client.request(method, path, args.get("data") if action != "get" else None)

    def _record_list(self, client, args):
        self._schema(client, args["collection"])
        return client.records(args["collection"], **{key: args[key] for key in
                              ("filter", "page", "per_page", "sort", "fields") if key in args})

    def records(self, args):
        client = self.client_factory()
        client.record_path(args.get("collection"), args.get("record_id") if args.get("action") in {"get", "update"} else None)
        dispatch = {"list": self._record_list, "get": self._record_direct,
                    "create": self._record_direct, "update": self._record_direct,
                    "preview_delete": self._preview_delete, "delete": self._delete}
        operation = dispatch.get(args.get("action"))
        if operation is None:
            raise ValueError("Unknown records action")
        result = operation(client, args)
        return {"success": result.get("approved", True) is not False,
                "data": sanitize(result, (client.password, client.token))}
