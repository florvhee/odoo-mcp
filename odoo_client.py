import xmlrpc.client
from dataclasses import dataclass


@dataclass
class OdooInstance:
    name: str
    url: str
    database: str
    username: str
    password: str


class OdooClient:
    def __init__(self, instance: OdooInstance):
        self._inst = instance
        self._uid: int | None = None

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self._inst.url}/xmlrpc/2/object")

    def _auth(self) -> int:
        if self._uid is None:
            common = xmlrpc.client.ServerProxy(f"{self._inst.url}/xmlrpc/2/common")
            uid = common.authenticate(
                self._inst.database, self._inst.username, self._inst.password, {}
            )
            if not uid:
                raise ValueError(
                    f"Authentication failed for instance '{self._inst.name}'. "
                    "Check your credentials in config.toml."
                )
            self._uid = uid
        return self._uid

    def search_read(self, model: str, domain: list, fields: list, limit: int = 100) -> list:
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, "search_read", [domain],
            {"fields": fields, "limit": limit},
        )

    def read(self, model: str, ids: list[int], fields: list) -> list:
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, "read", [ids], {"fields": fields},
        )

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, "write", [ids, values],
        )

    def create(self, model: str, values: dict) -> int:
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, "create", [values],
        )

    def search(self, model: str, domain: list, limit: int = 100) -> list[int]:
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, "search", [domain], {"limit": limit},
        )

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None):
        uid = self._auth()
        return self._models().execute_kw(
            self._inst.database, uid, self._inst.password,
            model, method, args, kwargs or {},
        )
