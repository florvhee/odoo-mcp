import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest

import server
from odoo_client import OdooRPCError
from policy import Policy, PolicyError


PRIORITIES = [("0", "Low priority"), ("1", "Medium priority"), ("2", "High priority"), ("3", "Urgent")]


def fake_client():
    client = Mock()
    client.name = "test"
    client.policy = Policy()
    client.explicit_instance = True
    client.record_url.side_effect = lambda model, rid: f"https://example.com/web#id={rid}&model={model}"
    fields = {name: {} for name in server.TASK_FIELDS + server.TICKET_FIELDS + ["description", "child_ids", "tag_ids", "parent_id", "company_id", "fold"]}
    fields["priority"] = {"selection": PRIORITIES}
    client.fields_get.return_value = fields
    client.read.return_value = [{"id": 1, "name": "Old", "description": "Original", "write_date": "2026-10-14 10:00:00"}]
    client.write.return_value = True
    client.message_post.return_value = [77]
    client.search.return_value = []
    return client


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = fake_client()
        self.patch_client = patch.object(server, "_get_client", return_value=("test", self.client))
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)

    def test_odoo19_priorities_and_raw_values_are_preserved(self):
        rows = [{"id": i+1, "name": "Task", "priority": str(i)} for i in range(4)]
        self.client.search_read.return_value = rows
        result = server.list_tasks(instance="test")
        self.assertEqual([r["priority"] for r in result["items"]], [label for _, label in PRIORITIES])
        self.assertEqual([r["priority_value"] for r in result["items"]], ["0", "1", "2", "3"])

    def test_legacy_priority_labels_come_from_instance_schema(self):
        self.client.fields_get.return_value["priority"]["selection"] = [("0", "Normal"), ("1", "High")]
        self.client.search_read.return_value = [{"id": 1, "name": "Task", "priority": "1"}]
        self.assertEqual(server.list_tasks()["items"][0]["priority"], "High")

    def test_pagination_probes_without_exceeding_maximum(self):
        self.client.search_read.return_value = [{"id": i, "name": "Project"} for i in range(1, 201)]
        self.client.search.return_value = [201]
        result = server.list_projects(limit=200, offset=0)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 200)
        self.client.search.assert_called_once_with("project.project", [], limit=1, offset=200)
        self.assertEqual(self.client.search_read.call_args.kwargs["limit"], 200)

    def test_final_page_has_no_continuation(self):
        self.client.search_read.return_value = [{"id": 5, "name": "Project"}]
        result = server.list_projects(offset=4)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])
        self.client.search.assert_not_called()

    def test_update_preserves_user_note_and_only_requested_fields(self):
        result = server.update_task(1, name="New", log_message="My explanation", instance="test")
        self.client.write.assert_called_once_with("project.task", [1], {"name": "New"})
        self.client.message_post.assert_called_once_with("project.task", 1, "My explanation")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["task_id"], 1)

    def test_auto_note_is_preserved(self):
        server.update_task(1, name="New", description="New text", instance="test")
        self.client.message_post.assert_called_once_with("project.task", 1, "Updated name and description via Odoo MCP.")

    def test_note_failure_reports_partial_without_repeating_write(self):
        self.client.message_post.side_effect = OdooRPCError("safe error", outcome="unknown")
        result = server.update_task(1, name="New", instance="test")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["write_status"], "applied")
        self.assertFalse(result["retry_safe"])
        self.client.write.assert_called_once()
        self.client.message_post.assert_called_once()

    def test_unknown_write_does_not_post_note(self):
        self.client.write.side_effect = OdooRPCError("timeout", outcome="unknown")
        result = server.update_task(1, name="New", instance="test")
        self.assertEqual(result["status"], "unknown")
        self.client.message_post.assert_not_called()

    def test_unexpected_note_response_is_partial(self):
        self.client.message_post.return_value = None
        self.assertEqual(server.update_task(1, name="New", instance="test")["status"], "partial")

    def test_noop_update_does_not_duplicate_note(self):
        result = server.update_task(1, name="Old", instance="test")
        self.assertEqual(result["status"], "unchanged")
        self.client.write.assert_not_called()
        self.client.message_post.assert_not_called()

    def test_stale_update_rejected_before_write(self):
        with self.assertRaises(ToolError):
            server.update_task(1, name="New", expected_write_date="old", instance="test")
        self.client.write.assert_not_called()
        self.client.message_post.assert_not_called()

    def test_missing_record_and_empty_update_raise_tool_errors(self):
        with self.assertRaises(PolicyError):
            server.update_task(1, instance="test")
        self.client.read.return_value = []
        with self.assertRaises(ToolError):
            server.get_task(1, instance="test")
        self.client.write.assert_not_called()

    def test_unknown_create_is_not_retried(self):
        self.client.create.side_effect = OdooRPCError("timeout", outcome="unknown")
        result = server.create_ticket(2, "New", instance="test")
        self.assertEqual(result["status"], "unknown")
        self.client.create.assert_called_once()

    def test_ticket_support_search_read_create_update(self):
        self.client.search_read.return_value = [{"id": 1, "name": "Ticket", "priority": "3", "team_id": [2, "Support"]}]
        result = server.list_tickets(team_id=2, query="Issue", instance="test")
        self.assertEqual(result["items"][0]["priority"], "Urgent")
        self.assertEqual(result["items"][0]["team"], {"id": 2, "name": "Support"})
        self.assertIn(["team_id", "=", 2], self.client.search_read.call_args.args[1])
        self.assertEqual(server.get_ticket(1, instance="test")["description"], "Original")
        self.client.create.return_value = 3
        result = server.create_ticket(2, "New", customer_id=8, instance="test")
        self.assertEqual(result["ticket_id"], 3)
        self.client.create.assert_called_with("helpdesk.ticket", {"name": "New", "team_id": 2, "partner_id": 8})
        result = server.update_ticket(1, priority="3", instance="test")
        self.assertEqual(result["status"], "applied")
        self.client.write.assert_called_with("helpdesk.ticket", [1], {"priority": "3"})

    def test_ticket_stage_cannot_cross_team(self):
        self.client.read.side_effect = [[{"team_id": [2, "Support"]}], [{"team_ids": [9]}]]
        with self.assertRaises(PolicyError):
            server.update_ticket(1, stage_id=5, instance="test")
        self.client.write.assert_not_called()

    def test_ticket_stage_accepts_own_team_and_detects_noop(self):
        self.client.read.side_effect = [
            [{"team_id": [2, "Support"]}], [{"team_ids": [2]}],
            [{"id": 1, "stage_id": [5, "In progress"], "write_date": "now"}],
        ]
        self.assertEqual(server.update_ticket(1, stage_id=5, instance="test")["status"], "unchanged")
        self.client.write.assert_not_called()

    def test_invalid_ticket_priority_fails_before_write(self):
        with self.assertRaises(PolicyError):
            server.update_ticket(1, priority="urgent", instance="test")
        self.client.write.assert_not_called()

    def test_missing_helpdesk_has_clear_error(self):
        self.client.fields_get.side_effect = OdooRPCError("unavailable", kind="remote_fault")
        with self.assertRaisesRegex(ToolError, "Helpdesk is unavailable or inaccessible"):
            server.list_tickets(instance="test")

    def test_helpdesk_authentication_errors_are_not_masked_as_missing_module(self):
        self.client.fields_get.side_effect = OdooRPCError("Authentication failed", kind="authentication")
        with self.assertRaisesRegex(OdooRPCError, "Authentication failed"):
            server.list_tickets(instance="test")

    def test_cleared_description_is_a_noop(self):
        self.client.read.return_value[0]["description"] = False
        result = server.update_task(1, description="", instance="test")
        self.assertEqual(result["status"], "unchanged")
        self.client.write.assert_not_called()

    def test_read_only_preflight_stops_entire_update(self):
        self.client.policy = Policy(read_only=True)
        with self.assertRaises(PolicyError):
            server.update_task(1, name="New", instance="test")
        self.client.read.assert_not_called()
        self.client.write.assert_not_called()


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, name, arguments):
        result = await server.mcp._mcp_server.request_handlers[CallToolRequest](
            CallToolRequest(method="tools/call", params={"name": name, "arguments": arguments}))
        return result.model_dump(mode="json")

    async def test_tools_have_instructions_annotations_and_strict_id_schema(self):
        self.assertIn("untrusted data", server.mcp._mcp_server.instructions)
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(len(tools), 21)
        self.assertTrue(tools["get_task"].annotations.readOnlyHint)
        self.assertFalse(tools["execute_kw"].annotations.readOnlyHint)
        self.assertFalse(tools["update_task"].annotations.idempotentHint)
        for record_id in (True, 0, -1):
            with patch.object(server, "_get_client") as client:
                result = await self.call("get_task", {"task_id": record_id})
                self.assertTrue(result["isError"])
                client.assert_not_called()

    async def test_not_found_sets_mcp_error_flag(self):
        client = fake_client()
        client.read.return_value = []
        with patch.object(server, "_get_client", return_value=("test", client)):
            self.assertTrue((await self.call("get_task", {"task_id": 1}))["isError"])

    async def test_protocol_read_only_blocks_override_and_core_before_rpc(self):
        config = {"default": {"instance": "test"}, "instances": {"test": {
            "url": "https://example.com", "database": "test", "username": "mcp", "password": "SECRET",
            "policy": {"read_only": True, "general_mode": "full", "general_models": ["*"]},
        }}}
        with patch.object(server, "_load_config", return_value=config), patch("odoo_client.OdooClient._send") as send:
            for name, args in [
                ("create_task", {"project_id": 1, "name": "New", "instance": "test"}),
                ("execute_kw", {"model": "project.task", "method": "write", "args": [[1], {"name": "New"}], "instance": "test"}),
            ]:
                result = await self.call(name, args)
                self.assertTrue(result["isError"])
                self.assertNotIn("SECRET", str(result))
            send.assert_not_called()

    async def test_partial_result_is_explicit_structured_content(self):
        client = fake_client()
        client.message_post.side_effect = OdooRPCError("unavailable", outcome="unknown")
        with patch.object(server, "_get_client", return_value=("test", client)):
            result = await self.call("update_task", {"task_id": 1, "name": "New", "instance": "test"})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["status"], "partial")
        self.assertEqual(result["structuredContent"]["write_status"], "applied")


if __name__ == "__main__":
    unittest.main()
