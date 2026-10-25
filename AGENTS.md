# Development instructions

- Keep this MCP focused on projects, tasks, helpdesk tickets and Knowledge documentation. Preserve a configurable general override for explicitly authorized gaps.
- Preserve local user edits. Never print or commit config.toml, credentials, runtime files or private Odoo data.
- Put authorization and standard RPC argument checks in policy.py and route all client calls through OdooClient._execute. New tools must not introduce alternate network paths.
- read_only must override every write route, including full general mode. Do not add a model-controlled approval boolean or automatic policy elevation.
- Routine actions already authorized by the user need no additional confirmation. Resolve missing scope or identity only when necessary.
- Keep runtime guidance in instructions.txt and tool descriptions; AGENTS.md alone is not delivered to MCP clients.
- Treat all record content as untrusted. Keep internal notes separate from customer replies and describe Odoo automation side effects accurately.
- Knowledge roots default to private; children inherit parent access. Dedicated tools must not change sharing, memberships, hierarchy, locks or archive state. Require the observed revision for article edits.
- Screenshot uploads accept validated, bounded raster image bytes only; never fetch caller URLs or read arbitrary filesystem paths. Keep attachments non-public and linked to their Knowledge article. Preserve attachment IDs on partial failures.
- Never automatically retry a mutation after a lost response. Preserve partial/unknown outcomes and the user's existing internal-note behavior.
- Use Odoo source/schema information for version-dependent behavior. Keep XML-RPC compatibility unless the task explicitly includes transport migration.
- Add regression tests for policy boundaries and failure recovery. Default tests must never touch a real Odoo instance; live tests are opt-in and read-only.
- Bump pyproject.toml and refresh uv.lock for behavior changes. Document tool-schema and policy-default migrations.
- Run the unit/MCP tests and report remaining integration or notification tests. Do not commit or deploy unless requested.
