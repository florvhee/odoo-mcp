"""Opt-in read-only checks; never mutate a configured Odoo database."""
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

import server


@unittest.skipUnless(os.environ.get("ODOO_MCP_TEST_INSTANCE"), "opt-in live instance not configured")
class LiveReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = os.environ["ODOO_MCP_TEST_INSTANCE"]
        config_path = Path(os.environ.get("ODOO_MCP_TEST_CONFIG", str(server.CONFIG_PATH)))
        with patch.object(server, "CONFIG_PATH", config_path):
            _, cls.client = server._get_client(cls.instance)
        cls.client.policy = replace(cls.client.policy, read_only=True, general_mode="disabled")
        cls.client._auth()  # Fail once on invalid credentials rather than misclassifying missing capabilities.

    def setUp(self):
        self.client_patch = patch.object(server, "_get_client", return_value=(self.instance, self.client))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def test_project_page(self):
        result = server.list_projects(instance=self.instance, limit=1)
        self.assertEqual(result["instance"], self.instance)
        self.assertLessEqual(len(result["items"]), 1)
        self.assertIn("next_offset", result)

    def test_task_priority_and_details(self):
        result = server.list_tasks(instance=self.instance, limit=1)
        if result["items"]:
            task = server.get_task(result["items"][0]["id"], instance=self.instance)
            choices = dict(self.client.fields_get("project.task")["priority"]["selection"])
            self.assertEqual(task["priority"], choices[task["priority_value"]])
            self.assertIn("url", task)

    def test_helpdesk_page(self):
        try:
            result = server.list_tickets(instance=self.instance, limit=1)
        except ToolError as exc:
            if "Helpdesk is unavailable or inaccessible" in str(exc):
                self.skipTest(str(exc))
            raise
        self.assertLessEqual(len(result["items"]), 1)
        if result["items"]:
            ticket = server.get_ticket(result["items"][0]["id"], instance=self.instance)
            self.assertEqual(ticket["model"], "helpdesk.ticket")

    def test_knowledge_page(self):
        try:
            result = server.list_knowledge_articles(instance=self.instance, limit=1)
        except ToolError as exc:
            if "Knowledge is unavailable or inaccessible" in str(exc):
                self.skipTest(str(exc))
            raise
        self.assertLessEqual(len(result["items"]), 1)
        if result["items"]:
            article_id = result["items"][0]["id"]
            article = server.get_knowledge_article(article_id, instance=self.instance)
            self.assertIn("body", article)
            attachments = server.list_knowledge_attachments(article_id, instance=self.instance, limit=1)
            self.assertLessEqual(len(attachments["items"]), 1)


if __name__ == "__main__":
    unittest.main()
