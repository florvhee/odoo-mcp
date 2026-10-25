# Odoo MCP

A small stdio MCP server focused on projects, tasks, helpdesk tickets and Knowledge documentation. Dedicated tools handle routine workflows; a configurable general RPC override covers other authorized work. Odoo ACLs and record rules remain authoritative.

## Setup

Requires Python 3.11+ and `uv`:

```sh
uv sync --locked
cp config.example.toml config.toml
# Edit config.toml with your instance credentials.
uv run --locked python server.py
```

Point your MCP client's command at `uv`, with arguments `--directory`, the absolute project path, `run`, `--locked`, `python`, `server.py`. Restart the MCP connection after updating the server so instructions and tool schemas are refreshed.

Use a dedicated Odoo account with only the needed access, HTTPS for remote instances, and an API key where supported (in the existing `password` setting). `config.toml` stays ignored. The server does not expose credentials in tool results or RPC error messages.

## Tools

- Instances: `list_instances` returns effective policy and exact instance names.
- Projects: `list_projects`, `create_project`.
- Tasks: `list_tasks`, `search_tasks`, `get_task`, `create_task`, `update_task`.
- Helpdesk: `list_ticket_teams`, `list_ticket_stages`, `list_tickets` (including text search), `get_ticket`, `create_ticket`, `update_ticket`.
- Knowledge: `list_knowledge_articles`, `get_knowledge_article`, `create_knowledge_article`, `update_knowledge_article`, `list_knowledge_attachments`, `attach_knowledge_image`.
- General override: `execute_kw`.

Task updates preserve the existing name/description editing and automatic internal-note behavior. Ticket updates additionally accept a priority selection value and a stage belonging to the ticket's team. Description updates replace the HTML field; omit it to preserve it. The dedicated tools do not post customer replies. Standard Odoo automations remain active, including stage-triggered customer emails and notifications to internal followers.

Task and ticket priority labels are read from `fields_get`, with the raw selection in `priority_value`. This supports Odoo 19's four levels and older two-level task priorities without guessing. Optional read fields are discovered per instance. Helpdesk requires the model to be installed and accessible; missing access and missing modules are reported together rather than misidentified.

## Policy and the general override

Settings in `[policy]` apply globally. `[instances.NAME.policy]` overrides individual settings; other settings are inherited. Configuration is loaded for each tool call, and invalid/unknown policy keys fail closed.

| Setting | Default | Meaning |
| --- | --- | --- |
| `read_only` | `false` | Reject all non-read methods, including dedicated edits and full overrides, before authentication/RPC. |
| `general_mode` | `"read_only"` | `disabled`, `read_only`, or `full`; controls `execute_kw` only. |
| `general_models` | Workflow/lookup models | Exact model allowlist for the override; `["*"]` deliberately permits every model. |
| `require_explicit_instance` | `true` | Mutations must pass a resolved instance name. Reads may use the default. |
| `max_records` | `200` | Maximum standard search/read/write/delete/create batch size; configurable from 1 to 1000. |
| `timeout_seconds` | `30` | HTTP(S) socket timeout; configurable from 1 to 300 seconds. |

Default general models: `project.project`, `project.task`, `project.task.type`, `project.tags`, `helpdesk.ticket`, `helpdesk.team`, `helpdesk.stage`, `helpdesk.tag`, `res.users`, `res.partner`, and `knowledge.article`. `ir.attachment` is deliberately excluded from the default general override: dedicated attachment access is restricted to validated image creation and article-scoped metadata searches.

A full override for one instance:

```toml
[instances.local.policy]
general_mode = "full"
general_models = ["*"]
```

A narrower override can name only specific models. A read-only instance remains read-only even if `general_mode = "full"`. Setting `general_mode = "disabled"` does not disable dedicated workflow tools.

Full mode allows arbitrary public methods, including deletion and methods with messaging or other side effects. There is no model-supplied `confirmed` flag, policy-enabling tool, or per-call confirmation loop. The assistant should reuse the user's existing authorization and resolve ambiguity only when needed. Explicitly passing a known instance is not a request for user confirmation.

The read-method allowlist is `read`, `search`, `search_read`, `search_count`, and `fields_get`. Standard reads require explicit fields where applicable; searches have a positive bounded limit and deterministic default ordering. Positional arguments cannot bypass the same checks. Custom methods enabled in full mode cannot have their batch sizes or side effects inferred by this server. Custom Odoo overrides must preserve standard read semantics; this policy is not a sandbox for arbitrary Odoo code. Read access to a permitted model is not field-level redaction; use Odoo access controls and appropriate accounts.

## Results, pagination and recovery

List/search tools return an object with `instance`, `items`, `offset`, `limit`, `has_more`, and `next_offset`. Follow `next_offset` until it is null. Pages use ID ordering; they are not a transactional snapshot if records change while paging. Task details cap the subtask expansion and report `subtasks_truncated`; the generic read tools can retrieve further subtasks.

Mutation results have explicit status:

- `applied`: the operation, including its requested internal note, was confirmed.
- `unchanged`: the requested values were already present; no write or extra note was posted.
- `partial`: the record update succeeded but the note was not confirmed. Inspect the chatter; do not repeat the edit.
- `unknown`: a mutation was sent but its result could not be confirmed. Verify the record/chatter before retrying. Creations and arbitrary RPC methods are not inherently idempotent.

Validation, policy, authentication and read failures are MCP errors (`isError=true`). Partial/unknown outcomes are explicit result objects, avoiding the misleading implication that nothing happened. RPC fault bodies are not forwarded because they can contain internal data. The XML-RPC transport makes no automatic retries, including after disconnects. A socket timeout is not an overall operation deadline.

`expected_write_date` optionally checks the observed revision before task/ticket updates. This is a best-effort stale-read check, not atomic concurrency protection. Record updates and chatter notes are separate Odoo transactions. Atomic updates, strict idempotency and race-free revision checks would require a dedicated Odoo-side method.

The general override returns Odoo's raw result on success, with an explicit `unknown` result on an uncertain mutation. It is annotated conservatively as potentially destructive/non-idempotent because its effects depend on the chosen method. The instance model/method policy is also enforced when callers use the Python client directly.

## Instructions

`instructions.txt` is loaded into MCP server initialization; it is not just repository guidance. It explains scope, existing authorization, pagination, untrusted record content, internal/customer communication, and recovery. Tool annotations describe effects but do not replace policy enforcement. `AGENTS.md` provides development guidance.

## Knowledge documentation and screenshots

Requires Odoo Enterprise Knowledge and an account with the appropriate article permissions. The implementation follows the local Odoo 19 `knowledge.article` and `ir.attachment` APIs. Other Knowledge versions require integration validation.

Search articles with `list_knowledge_articles(query="Onboarding", instance="local")`, then read the selected ID with `get_knowledge_article`. Lists return metadata; details return `body` HTML, `write_date`, parent and available permission metadata. A parent filter lists direct children. Searches exclude templates and trashed articles when those fields exist.

Create documentation under a resolved parent:

```json
{"name":"Handling a support ticket","parent_id":42,"body":"<h1>Handling a support ticket</h1><p>First, open Helpdesk.</p>","instance":"local"}
```

Without `parent_id`, `create_knowledge_article` creates a private root owned by the connected Odoo account. Child articles inherit their parent's audience, including external members where applicable. The tools do not expose sharing, membership, moving, trashing or unlocking operations. Locked, archived, trashed, template and explicitly non-writable articles are rejected before dedicated edits/uploads.

`update_knowledge_article` accepts `name`, full replacement `body`, or `append_html` (mutually exclusive with `body`). It requires the observed `expected_write_date`. Example after reading article 123:

```json
{"article_id":123,"expected_write_date":"2026-10-15 10:00:00","append_html":"<h2>Escalation</h2><p>Assign the ticket to the support lead.</p>","instance":"local"}
```

Appending preserves existing HTML and image references. Replacing a body can remove Odoo editor structures; use it only for an authorized replacement. Revision checks are best-effort and not atomic locks. Edits add an internal note using the existing partial/unknown outcome handling; notes are not undo snapshots. Odoo 19's standard article body history remains managed by Odoo.

`attach_knowledge_image` accepts `article_id`, `filename`, `image_base64`, optional `alt_text`, and `instance`. It supports static PNG, JPEG and WebP up to 5 MiB and 20 megapixels. Pillow validates the decoded image and filename extension; SVG, arbitrary binary files, animations, malformed base64 and oversized images are rejected. The MCP server never reads caller-specified filesystem paths or fetches remote URLs. A trusted caller can encode an explicitly selected screenshot before submitting it:

```python
import base64
from pathlib import Path

# Use only a screenshot selected/authorized for this article's audience.
image_base64 = base64.b64encode(Path("screenshot.png").read_bytes()).decode("ascii")
arguments = {
    "article_id": 123, "filename": "screenshot.png", "image_base64": image_base64,
    "alt_text": "The Helpdesk ticket form", "expected_write_date": "2026-10-15 10:00:00",
    "instance": "local",
}
# Pass arguments to the MCP tool attach_knowledge_image.
```

By default `embed=true` appends an `<img>` to the article, preserving its existing body; pass the observed revision. `embed=false` uploads only and returns `image_html` for later placement. The attachment uses `res_model="knowledge.article"`, its exact `res_id`, and `public=false`; no access token or public sharing is created. Images use `/web/image/<attachment_id>` and the viewer's existing Odoo access. Standard internal access is the intended workflow; portal/anonymous rendering must be validated separately without weakening permissions.

Upload, body update and internal note are separate transactions. If upload succeeds but embedding/note fails, the result is `partial`, with `attachment_id`, `image_html`, `attachment_status` and `article_status`. Refresh the article and reuse the attachment; do not upload it again. If upload outcome is unknown, use `list_knowledge_attachments` (bounded metadata including checksum/size/date) to investigate first. Filenames alone are not unique. No automatic deletion/cleanup or retry is attempted.

## Upgrading from 0.2

- Version 0.3 adds six Knowledge tools (21 tools total), a Pillow dependency and Knowledge-specific runtime instructions. Run `uv sync --locked` and restart existing MCP connections.
- `knowledge.article` joins the default general read-model allowlist. An explicit `general_models` list remains unchanged. Attachments are not added to that list.
- Dedicated Knowledge writes follow the existing `read_only` and explicit-instance rules. `general_mode="read_only"` still controls only the general override, not dedicated writes.
- Your credentials and `config.toml` are not changed. No Odoo module/schema migration is required.

## Upgrading from 0.1

- Your existing credentials and instance names remain valid; `config.toml` is not rewritten.
- The previously unrestricted override now defaults to read-only access on workflow/lookup models. Explicitly configure `full` and the desired models if you need broader operations.
- Mutations require an explicit instance unless you deliberately disable that setting.
- List/search results changed from arrays to paginated objects. Update consumers to use `items` and `next_offset`.
- Priorities now use the instance's real selection labels, plus `priority_value`; do not compare the former `normal`/`high` strings.
- Task `parent_task` and subtask `stage` now include IDs and names; resources serialize the same structured results and include the instance.
- Missing records now produce MCP errors. Update consumers to inspect mutation `status`, not just `success`.
- Python dependencies are locked; MCP is restricted to the tested 1.x API family.

The transport remains XML-RPC for compatibility. JSON-2 support, richer task editing, chatter retrieval and project summaries are separate future changes.

## Tests

```sh
uv run --locked python -B -m unittest discover -s tests -v
```

The default suite uses mocks and MCP request handlers; it makes no Odoo network calls. It covers cross-route guardrails, positional-argument bypasses, read-only precedence, explicit-instance checks, transport retries, partial outcomes, priority versions, pagination, ticket stage scope, stale edits and MCP error/annotation behavior. Knowledge tests additionally cover private roots, inherited child permissions, HTML/image preservation, attachment scope, raster validation, concurrent edits and upload/embed/note failure recovery.

Optional read-only integration checks use your configured instance and force read-only mode:

```sh
ODOO_MCP_TEST_INSTANCE=local uv run --locked python -B -m unittest discover -s tests -p test_live.py -v
```

Use `ODOO_MCP_TEST_CONFIG` only if the test configuration is stored elsewhere. Integration tests do not print record contents or perform writes. Mutation behavior, Odoo account/company permissions and notification side effects still need validation in a disposable Odoo database before production rollout.
