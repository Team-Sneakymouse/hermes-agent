"""Host approval bridge; automatic grants are not human consent.

Observe the host's published human-response event as well as its decision. This
fails closed for yolo/off/unattended auto-approve and for missing observer hooks.
Random per-attempt rule keys ensure session/always cannot broaden operation scope.
"""
import threading
import uuid


class HumanApproval:
    def __init__(self, requester=None):
        self.requester = requester
        self._pending = {}
        self._lock = threading.Lock()

    def observe(self, pattern_key="", choice="", **kwargs):
        with self._lock:
            if pattern_key in self._pending and choice in {"once", "session", "always"}:
                self._pending[pattern_key] = True

    def confirm(self, tool_name, reason):
        from tools.approval import request_tool_approval
        rule_key = "pocketbase:" + uuid.uuid4().hex
        key = "plugin_rule:" + rule_key
        with self._lock:
            self._pending[key] = False
        try:
            result = (self.requester or request_tool_approval)(tool_name, reason, rule_key=rule_key)
            with self._lock:
                return result.get("approved") is True and self._pending[key]
        finally:
            with self._lock:
                self._pending.pop(key, None)
