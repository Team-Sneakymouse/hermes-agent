"""Small synchronous PocketBase REST client; no automatic write retries."""
import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from http.client import HTTPException


class PocketBaseError(RuntimeError):
    def __init__(self, status=None, method=None):
        self.status = status
        self.uncertain = method in {"POST", "PATCH", "PUT", "DELETE"} and (status is None or status >= 500)
        message = (f"PocketBase request failed (HTTP {status})" if status else
                   "PocketBase connection or response failed")
        if self.uncertain:
            message += "; write outcome may be uncertain; inspect the exact target and never automatically retry"
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PocketBaseClient:
    def __init__(self, host, username, password):
        parsed = urlsplit(host)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("PocketBase host must be HTTPS (HTTP only for local tests), without credentials/query/fragment")
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.token = None

    def request(self, method, path, data=None, params=None, authenticate=True):
        if authenticate and self.token is None:
            self.token = self.request("POST", "/api/collections/_superusers/auth-with-password",
                                      {"identity": self.username, "password": self.password},
                                      authenticate=False).get("token")
            if not isinstance(self.token, str) or not self.token:
                self.token = None
                raise PocketBaseError()
        url = self.host + path
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        if query:
            url += "?" + query
        headers = {"Content-Type": "application/json"}
        if authenticate:
            headers["Authorization"] = self.token
        body = json.dumps(data).encode() if data is not None else None
        try:
            with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers, method=method), timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            exc.close()
            raise PocketBaseError(exc.code, method) from None
        except (URLError, HTTPException, OSError, ValueError):
            raise PocketBaseError(method=method) from None

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_]*", value):
            raise ValueError("Invalid PocketBase identifier")
        return value

    @classmethod
    def record_path(cls, collection, record_id=None):
        cls.identifier(collection)
        if collection.startswith("_"):
            raise ValueError("System collections are not supported")
        path = f"/api/collections/{collection}/records"
        return path if record_id is None else path + "/" + cls.identifier(record_id)

    def records(self, collection, *, filter=None, page=1, per_page=100, sort=None, fields=None):
        path = self.record_path(collection)
        if type(page) is not int or page < 1 or type(per_page) is not int or not 1 <= per_page <= 500:
            raise ValueError("page must be positive; per_page must be between 1 and 500")
        return self.request("GET", path,
                            params={"filter": filter, "page": page, "perPage": per_page,
                                    "sort": sort, "fields": fields})
