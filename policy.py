"""Configuration-backed checks shared by dedicated tools and the RPC override."""

from dataclasses import dataclass, field, fields

from images import validate_image


READ_METHODS = frozenset({"read", "search", "search_read", "search_count", "fields_get"})
CORE_MODELS = frozenset({
    "project.project", "project.task", "project.task.type", "project.tags",
    "helpdesk.ticket", "helpdesk.team", "helpdesk.stage", "helpdesk.tag",
    "res.users", "res.partner", "knowledge.article",
})
CORE_WRITES = {
    "project.project": {"create"},
    "project.task": {"create", "write", "message_post"},
    "helpdesk.ticket": {"create", "write", "message_post"},
    "knowledge.article": {"create", "write", "message_post"},
}

ATTACHMENT_FIELDS = frozenset({"id", "name", "mimetype", "file_size", "checksum", "create_date", "res_model", "res_id"})


class PolicyError(ValueError):
    """A call was rejected before reaching Odoo."""


def positive_id(value: int, label: str = "record_id") -> int:
    if type(value) is not int or value < 1:
        raise PolicyError(f"{label} must be a positive integer.")
    return value


@dataclass(frozen=True)
class Policy:
    read_only: bool = False
    general_mode: str = "read_only"
    general_models: tuple[str, ...] = field(default_factory=lambda: tuple(sorted(CORE_MODELS)))
    require_explicit_instance: bool = True
    max_records: int = 200
    timeout_seconds: int = 30

    @classmethod
    def from_config(cls, defaults: dict | None = None, overrides: dict | None = None):
        settings = {}
        for source in (defaults, overrides):
            if source is not None and not isinstance(source, dict):
                raise PolicyError("policy must be a TOML table.")
            settings.update(source or {})
        if set(settings) - {f.name for f in fields(cls)}:
            raise PolicyError("Unknown policy setting; check the documented configuration keys.")
        for name in ("read_only", "require_explicit_instance"):
            if name in settings and type(settings[name]) is not bool:
                raise PolicyError(f"{name} must be a boolean.")
        if settings.get("general_mode", "read_only") not in {"disabled", "read_only", "full"}:
            raise PolicyError("general_mode must be disabled, read_only, or full.")
        for name, maximum in (("max_records", 1000), ("timeout_seconds", 300)):
            if name in settings:
                positive_id(settings[name], name)
                if settings[name] > maximum:
                    raise PolicyError(f"{name} must be at most {maximum}.")
        if "general_models" in settings:
            models = settings["general_models"]
            if not isinstance(models, list) or not all(isinstance(m, str) and m.strip() for m in models):
                raise PolicyError("general_models must be a list of model names, or ['*'].")
            settings["general_models"] = tuple(models)
        return cls(**settings)

    def authorize(self, model: str, method: str, *, general: bool, explicit_instance: bool):
        if not isinstance(model, str) or not model or not isinstance(method, str) or not method:
            raise PolicyError("A model and method are required.")
        if method.startswith("_"):
            raise PolicyError("Private methods cannot be called.")
        mutation = method not in READ_METHODS
        if mutation and self.read_only:
            raise PolicyError("This instance is read-only.")
        if mutation and self.require_explicit_instance and not explicit_instance:
            raise PolicyError("Pass the resolved instance name explicitly for mutations.")
        if general:
            if self.general_mode == "disabled":
                raise PolicyError("The general override is disabled for this instance.")
            if "*" not in self.general_models and model not in self.general_models:
                raise PolicyError("This model is not enabled for the general override.")
            if mutation and self.general_mode != "full":
                raise PolicyError("General writes require general_mode = 'full' in trusted configuration.")
        elif model == "ir.attachment":
            if method not in {"create", "search", "search_read"}:
                raise PolicyError("Dedicated attachment access only supports image creation and scoped metadata search.")
        elif model not in CORE_MODELS or (mutation and method not in CORE_WRITES.get(model, set())):
            raise PolicyError("This operation is outside the dedicated workflow tools.")

    def page(self, limit: int, offset: int):
        positive_id(limit, "limit")
        if limit > self.max_records:
            raise PolicyError(f"limit must be at most {self.max_records}.")
        if type(offset) is not int or offset < 0:
            raise PolicyError("offset must be a non-negative integer.")

    def ids(self, ids):
        if not isinstance(ids, (list, tuple)) or not ids or len(ids) > self.max_records:
            raise PolicyError(f"Provide between 1 and {self.max_records} record IDs.")
        for record_id in ids:
            positive_id(record_id)

    def prepare(self, method: str, args: list, kwargs: dict | None, *, model=None, general=False) -> tuple[list, dict]:
        """Normalize standard read signatures so positional arguments cannot bypass bounds."""
        if not isinstance(args, list) or (kwargs is not None and not isinstance(kwargs, dict)):
            raise PolicyError("args must be a list and kwargs must be an object.")
        kwargs = dict(kwargs or {})
        signatures = {
            "search": ("domain", "offset", "limit", "order"),
            "search_read": ("domain", "fields", "offset", "limit", "order"),
            "search_count": ("domain", "limit"),
            "read": ("ids", "fields"),
            "fields_get": ("allfields", "attributes"),
        }
        if method in signatures:
            names = signatures[method]
            if len(args) > len(names):
                raise PolicyError("Too many positional arguments for this read method.")
            values = dict(zip(names, args))
            if values.keys() & kwargs.keys():
                raise PolicyError("Do not supply an argument both positionally and by keyword.")
            values.update(kwargs)
            if method in {"search", "search_read"}:
                self.page(values.setdefault("limit", min(50, self.max_records)), values.setdefault("offset", 0))
                values.setdefault("order", "id asc")
            if method in {"search", "search_read", "search_count"}:
                values.setdefault("domain", [])
                if not isinstance(values["domain"], list):
                    raise PolicyError("domain must be a list.")
            if method in {"read", "search_read"}:
                selected = values.get("fields")
                if not isinstance(selected, list) or not selected or not all(isinstance(f, str) and f for f in selected):
                    raise PolicyError("Provide an explicit non-empty fields list.")
            if method == "read":
                self.ids(values.get("ids"))
                return [values.pop("ids")], values
            if model == "ir.attachment" and not general:
                domain = values.get("domain", [])
                # A fixed AND scope prevents OR/NOT domains from escaping the selected article.
                if (len(domain) != 3 or ["res_model", "=", "knowledge.article"] not in domain
                        or ["type", "=", "binary"] not in domain):
                    raise PolicyError("Attachment metadata must be scoped to one Knowledge article.")
                article_terms = [v for v in domain if isinstance(v, list) and len(v) == 3 and v[:2] == ["res_id", "="]]
                if len(article_terms) != 1:
                    raise PolicyError("Attachment metadata requires an exact article ID.")
                positive_id(article_terms[0][2], "article_id")
                if set(values) - {"domain", "fields", "offset", "limit", "order"}:
                    raise PolicyError("Extra attachment search options are not supported.")
                if method == "search_read" and set(values["fields"]) - ATTACHMENT_FIELDS:
                    raise PolicyError("Dedicated attachment searches expose metadata only.")
            return [], values
        if method in {"write", "unlink", "message_post"}:
            self.ids(args[0] if args else kwargs.get("ids"))
        if method == "create":
            values = args[0] if args else kwargs.get("vals_list", kwargs.get("vals"))
            if not isinstance(values, (dict, list)) or not values:
                raise PolicyError("create requires values.")
            if isinstance(values, list) and len(values) > self.max_records:
                raise PolicyError(f"create accepts at most {self.max_records} records.")
            if model == "ir.attachment" and not general:
                required = {"name", "datas", "mimetype", "res_model", "res_id", "type", "public"}
                if (not isinstance(values, dict) or set(values) != required
                        or values["res_model"] != "knowledge.article" or values["type"] != "binary"
                        or values["public"] is not False or kwargs):
                    raise PolicyError("Only a non-public image linked to a Knowledge article can be uploaded.")
                positive_id(values["res_id"], "article_id")
                try:
                    mime = validate_image(values["name"], values["datas"])
                except ValueError as exc:
                    raise PolicyError(str(exc)) from None
                if values["mimetype"] != mime:
                    raise PolicyError("Attachment MIME type must match the image bytes.")
        return list(args), kwargs
