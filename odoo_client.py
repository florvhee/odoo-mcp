import http.client
import xmlrpc.client
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit
from xml.parsers.expat import ExpatError

from policy import Policy, READ_METHODS


@dataclass
class OdooInstance:
    name: str
    url: str
    database: str
    username: str
    password: str = field(repr=False)


class OdooRPCError(RuntimeError):
    def __init__(self, message: str, *, outcome: str = "not_applied", kind: str = "rpc"):
        super().__init__(message)
        self.outcome = outcome
        self.kind = kind


class _NoRetryTransport:
    """XML-RPC's default transport retries disconnected requests, including writes."""

    def __init__(self, timeout: int):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection

    def request(self, host, handler, request_body, verbose=False):
        return self.single_request(host, handler, request_body, verbose)


class _HTTPTransport(_NoRetryTransport, xmlrpc.client.Transport):
    pass


class _HTTPSTransport(_NoRetryTransport, xmlrpc.client.SafeTransport):
    pass


class OdooClient:
    def __init__(self, instance: OdooInstance, policy: Policy | None = None, *, explicit_instance: bool = False):
        self._inst = instance
        self.policy = policy or Policy()
        self.explicit_instance = explicit_instance
        self._uid: int | None = None
        self._field_cache: dict[str, dict] = {}
        parsed = urlsplit(instance.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Instance URL must be HTTP(S), without embedded credentials.")

    @property
    def name(self):
        return self._inst.name

    def record_url(self, model: str, record_id: int):
        if model == "knowledge.article":
            return f"{self._inst.url}/knowledge/article/{record_id}"
        return f"{self._inst.url}/web#" + urlencode({"id": record_id, "model": model, "view_type": "form"})

    def image_url(self, attachment_id: int):
        return f"{self._inst.url}/web/image/{attachment_id}"

    def current_partner_id(self):
        rows = self.read("res.users", [self._auth()], ["partner_id"])
        if not rows or not rows[0].get("partner_id"):
            raise ValueError("The authenticated Odoo user has no accessible partner.")
        return rows[0]["partner_id"][0]

    def _send(self, service: str, method: str, *args):
        transport_cls = _HTTPSTransport if urlsplit(self._inst.url).scheme == "https" else _HTTPTransport
        with xmlrpc.client.ServerProxy(
            f"{self._inst.url}/xmlrpc/2/{service}",
            transport=transport_cls(self.policy.timeout_seconds),
        ) as proxy:
            return getattr(proxy, method)(*args)

    def _auth(self) -> int:
        if self._uid is None:
            try:
                uid = self._send("common", "authenticate", self._inst.database, self._inst.username, self._inst.password, {})
            except (xmlrpc.client.Error, OSError, http.client.HTTPException, ExpatError):
                raise OdooRPCError(f"Authentication unavailable for instance '{self.name}'. No operation was sent.", kind="authentication") from None
            if not uid:
                raise OdooRPCError(f"Authentication failed for instance '{self.name}'. Check its credentials.", kind="authentication")
            self._uid = uid
        return self._uid

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None, *, general=False):
        self.policy.authorize(model, method, general=general, explicit_instance=self.explicit_instance)
        args, kwargs = self.policy.prepare(method, args, kwargs, model=model, general=general)
        # Reject unencodable values before sending a mutation.
        try:
            xmlrpc.client.dumps((args, kwargs), allow_none=False)
        except (TypeError, OverflowError):
            raise ValueError("Arguments cannot be encoded as XML-RPC. Use false to clear Odoo fields, not null.") from None
        uid = self._auth()
        try:
            return self._send("object", "execute_kw", self._inst.database, uid, self._inst.password, model, method, args, kwargs)
        except (xmlrpc.client.Error, OSError, http.client.HTTPException, ExpatError) as exc:
            # A response fault can occur after commit; it does not prove rollback.
            outcome = "not_applied" if method in READ_METHODS else "unknown"
            raise OdooRPCError(
                f"Odoo call {model}.{method} failed on instance '{self.name}'. "
                + ("Outcome unknown; verify before retrying." if outcome == "unknown" else "Check availability and access rights."),
                outcome=outcome,
                kind="remote_fault" if isinstance(exc, xmlrpc.client.Fault) else "transport",
            ) from None

    def search_read(self, model: str, domain: list, fields: list, limit: int = 50, offset: int = 0, order: str = "id asc") -> list:
        return self._execute(model, "search_read", [domain], {"fields": fields, "limit": limit, "offset": offset, "order": order})

    def read(self, model: str, ids: list[int], fields: list) -> list:
        return self._execute(model, "read", [ids], {"fields": fields})

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self._execute(model, "write", [ids, values])

    def create(self, model: str, values: dict) -> int:
        return self._execute(model, "create", [values])

    def search(self, model: str, domain: list, limit: int = 50, offset: int = 0) -> list[int]:
        return self._execute(model, "search", [domain], {"limit": limit, "offset": offset})

    def message_post(self, model: str, record_id: int, body: str) -> list[int]:
        return self._execute(model, "message_post", [[record_id]], {
            "body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note",
        })

    def fields_get(self, model: str) -> dict:
        if model not in self._field_cache:
            self._field_cache[model] = self._execute(model, "fields_get", [], {
                "attributes": ["type", "selection", "string"],
            })
        return self._field_cache[model]

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self._execute(model, method, args, kwargs, general=True)
