import tomllib
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from odoo_client import OdooClient, OdooInstance

mcp = FastMCP("odoo")

CONFIG_PATH = Path(__file__).parent / "config.toml"

# ── Config loading ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.toml not found at {CONFIG_PATH}. "
            "Copy config.example.toml to config.toml and fill in your credentials."
        )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def _get_client(instance_name: str | None = None) -> tuple[str, OdooClient]:
    config = _load_config()
    if instance_name is None:
        instance_name = config.get("default", {}).get("instance")
        if not instance_name:
            raise ValueError("No default instance set in config.toml and no instance specified.")
    instances = config.get("instances", {})
    if instance_name not in instances:
        available = list(instances.keys())
        raise ValueError(f"Instance '{instance_name}' not found. Available: {available}")
    cfg = instances[instance_name]
    inst = OdooInstance(
        name=instance_name,
        url=cfg["url"].rstrip("/"),
        database=cfg["database"],
        username=cfg["username"],
        password=cfg["password"],
    )
    return instance_name, OdooClient(inst)


# ── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_instances() -> list[dict]:
    """List all configured Odoo instances."""
    config = _load_config()
    default = config.get("default", {}).get("instance")
    return [
        {"name": name, "url": cfg["url"], "database": cfg["database"], "default": name == default}
        for name, cfg in config.get("instances", {}).items()
    ]


@mcp.tool()
def list_projects(instance: str | None = None) -> list[dict]:
    """List all projects in an Odoo instance.

    Args:
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    _, client = _get_client(instance)
    projects = client.search_read(
        "project.project",
        [],
        ["id", "name", "task_count", "description", "partner_id"],
        limit=200,
    )
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "task_count": p.get("task_count", 0),
            "client": p["partner_id"][1] if p.get("partner_id") else None,
        }
        for p in projects
    ]


@mcp.tool()
def list_tasks(
    project_id: int | None = None,
    stage: str | None = None,
    assignee: str | None = None,
    limit: int = 50,
    instance: str | None = None,
) -> list[dict]:
    """List tasks, optionally filtered by project, stage, or assignee.

    Args:
        project_id: Filter by project ID (get IDs from list_projects).
        stage: Filter by stage name (e.g. 'In Progress', 'Done').
        assignee: Filter by assignee name (partial match).
        limit: Max number of tasks to return (default 50).
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    _, client = _get_client(instance)
    domain: list = []
    if project_id is not None:
        domain.append(["project_id", "=", project_id])
    if stage:
        domain.append(["stage_id.name", "ilike", stage])
    if assignee:
        domain.append(["user_ids.name", "ilike", assignee])

    tasks = client.search_read(
        "project.task",
        domain,
        ["id", "name", "stage_id", "user_ids", "project_id", "date_deadline", "priority"],
        limit=limit,
    )
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "project": t["project_id"][1] if t.get("project_id") else None,
            "stage": t["stage_id"][1] if t.get("stage_id") else None,
            "assignees": [u[1] for u in t.get("user_ids", [])] if t.get("user_ids") else [],
            "deadline": t.get("date_deadline"),
            "priority": "high" if t.get("priority") == "1" else "normal",
        }
        for t in tasks
    ]


@mcp.tool()
def get_task(task_id: int, instance: str | None = None) -> dict:
    """Get full details of a specific task including description and subtasks.

    Args:
        task_id: The numeric ID of the task.
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    _, client = _get_client(instance)
    results = client.read(
        "project.task",
        [task_id],
        [
            "id", "name", "description", "stage_id", "user_ids", "project_id",
            "date_deadline", "priority", "tag_ids", "child_ids", "parent_id",
        ],
    )
    if not results:
        return {"error": f"Task {task_id} not found."}
    t = results[0]

    # Fetch subtask names if any
    subtasks = []
    if t.get("child_ids"):
        sub_results = client.read("project.task", t["child_ids"], ["id", "name", "stage_id"])
        subtasks = [
            {"id": s["id"], "name": s["name"], "stage": s["stage_id"][1] if s.get("stage_id") else None}
            for s in sub_results
        ]

    # Fetch tag names if any
    tags = []
    if t.get("tag_ids"):
        tag_results = client.read("project.tags", t["tag_ids"], ["id", "name"])
        tags = [tag["name"] for tag in tag_results]

    return {
        "id": t["id"],
        "name": t["name"],
        "description": t.get("description") or "",
        "project": t["project_id"][1] if t.get("project_id") else None,
        "stage": t["stage_id"][1] if t.get("stage_id") else None,
        "assignees": [u[1] for u in t.get("user_ids", [])] if t.get("user_ids") else [],
        "deadline": t.get("date_deadline"),
        "priority": "high" if t.get("priority") == "1" else "normal",
        "tags": tags,
        "parent_task": t["parent_id"][1] if t.get("parent_id") else None,
        "subtasks": subtasks,
    }


@mcp.tool()
def search_tasks(query: str, project_id: int | None = None, instance: str | None = None) -> list[dict]:
    """Search tasks by keyword in name or description.

    Args:
        query: Search term to look for in task names and descriptions.
        project_id: Optionally restrict search to a specific project.
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    _, client = _get_client(instance)
    domain: list = ["|", ["name", "ilike", query], ["description", "ilike", query]]
    if project_id is not None:
        domain = [["project_id", "=", project_id]] + domain

    tasks = client.search_read(
        "project.task",
        domain,
        ["id", "name", "stage_id", "project_id", "user_ids"],
        limit=50,
    )
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "project": t["project_id"][1] if t.get("project_id") else None,
            "stage": t["stage_id"][1] if t.get("stage_id") else None,
            "assignees": [u[1] for u in t.get("user_ids", [])] if t.get("user_ids") else [],
        }
        for t in tasks
    ]


@mcp.tool()
def update_task(
    task_id: int,
    name: str | None = None,
    description: str | None = None,
    instance: str | None = None,
) -> dict:
    """Update the name and/or description of a task in Odoo.

    Args:
        task_id: The numeric ID of the task to update.
        name: New task name. Omit to leave unchanged.
        description: New task description (HTML supported). Omit to leave unchanged.
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    if name is None and description is None:
        return {"error": "Provide at least one of: name, description."}
    _, client = _get_client(instance)
    values: dict = {}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    client.write("project.task", [task_id], values)
    return {"success": True, "task_id": task_id, "updated_fields": list(values.keys())}


@mcp.tool()
def create_task(
    project_id: int,
    name: str,
    description: str | None = None,
    instance: str | None = None,
) -> dict:
    """Create a new task in a project.

    Args:
        project_id: The numeric ID of the project (get from list_projects).
        name: Task name.
        description: Optional task description (HTML supported).
        instance: Instance name from config.toml. Uses the default if omitted.
    """
    _, client = _get_client(instance)
    values: dict = {"project_id": project_id, "name": name}
    if description:
        values["description"] = description
    task_id = client.create("project.task", values)
    return {"success": True, "task_id": task_id, "name": name, "project_id": project_id}


# ── Resources ───────────────────────────────────────────────────────────────

@mcp.resource("odoo://instances")
def resource_instances() -> str:
    """All configured Odoo instances."""
    instances = list_instances()
    lines = [f"- {i['name']} ({i['url']} / {i['database']})" + (" [default]" if i["default"] else "") for i in instances]
    return "\n".join(lines)


@mcp.resource("odoo://projects")
def resource_projects() -> str:
    """All projects in the default Odoo instance."""
    projects = list_projects()
    lines = [f"- [{p['id']}] {p['name']} ({p['task_count']} tasks)" + (f" — {p['client']}" if p.get("client") else "") for p in projects]
    return "\n".join(lines)


@mcp.resource("odoo://task/{task_id}")
def resource_task(task_id: int) -> str:
    """Full details of a specific task."""
    task = get_task(task_id)
    if "error" in task:
        return task["error"]
    parts = [
        f"# [{task['id']}] {task['name']}",
        f"**Project:** {task.get('project')}",
        f"**Stage:** {task.get('stage')}",
        f"**Assignees:** {', '.join(task['assignees']) or 'None'}",
        f"**Priority:** {task.get('priority')}",
        f"**Deadline:** {task.get('deadline') or 'None'}",
        f"**Tags:** {', '.join(task['tags']) or 'None'}",
        "",
        "## Description",
        task.get("description") or "_No description_",
    ]
    if task["subtasks"]:
        parts += ["", "## Subtasks"]
        parts += [f"- [{s['id']}] {s['name']} ({s['stage']})" for s in task["subtasks"]]
    return "\n".join(parts)


if __name__ == "__main__":
    mcp.run(transport="stdio")
