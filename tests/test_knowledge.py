import base64
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest

import images
import server
from odoo_client import OdooRPCError
from policy import Policy, PolicyError
from test_guardrails import make_client
from test_server import fake_client


REVISION = "2026-10-15 10:00:00"


def screenshot(fmt="PNG"):
    stream = BytesIO()
    Image.new("RGB", (2, 2), "white").save(stream, format=fmt)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def attachment_values():
    return {"name": "screen.png", "datas": screenshot(), "mimetype": "image/png",
            "res_model": "knowledge.article", "res_id": 1, "type": "binary", "public": False}


class ImageValidationTests(unittest.TestCase):
    def test_supported_formats_are_detected(self):
        for fmt, suffix, mime in [("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp")]:
            with self.subTest(fmt=fmt):
                self.assertEqual(images.validate_image(f"screen.{suffix}", screenshot(fmt)), mime)

    def test_invalid_content_paths_and_mismatched_extension_are_rejected(self):
        cases = [("screen.png", "invalid!"), ("screen.png", base64.b64encode(b"<svg/>").decode()),
                 ("screen.png", base64.b64encode(b"<html>secret</html>").decode()),
                 ("screen.jpg", screenshot()), ("/tmp/secret.png", screenshot()),
                 ("..\\secret.png", screenshot()), ("screen\n.png", screenshot()),
                 ("screen.png", "data:image/png;base64," + screenshot())]
        for filename, data in cases:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                images.validate_image(filename, data)

    def test_byte_and_pixel_limits_and_truncation(self):
        with patch.object(images, "MAX_IMAGE_BYTES", 1), self.assertRaises(ValueError):
            images.validate_image("screen.png", screenshot())
        with patch.object(images, "MAX_BASE64_LENGTH", 1), self.assertRaises(ValueError):
            images.validate_image("screen.png", screenshot())
        with patch.object(images, "MAX_IMAGE_PIXELS", 1), self.assertRaises(ValueError):
            images.validate_image("screen.png", screenshot())
        truncated = base64.b64encode(base64.b64decode(screenshot())[:40]).decode()
        with self.assertRaises(ValueError):
            images.validate_image("screen.png", truncated)

    def test_animated_images_are_rejected(self):
        stream = BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, format="PNG", save_all=True,
                                            append_images=[Image.new("RGB", (2, 2), "blue")])
        with self.assertRaises(ValueError):
            images.validate_image("animation.png", base64.b64encode(stream.getvalue()).decode())


class KnowledgePolicyTests(unittest.TestCase):
    def test_read_only_and_implicit_instance_block_new_writes_before_network(self):
        for policy, explicit in [(Policy(read_only=True, general_mode="full", general_models=("*",)), True),
                                  (Policy(), False)]:
            client = make_client(policy, explicit)
            calls = [lambda: client.create("knowledge.article", {"name": "X"}),
                     lambda: client.write("knowledge.article", [1], {"body": "X"}),
                     lambda: client.message_post("knowledge.article", 1, "note"),
                     lambda: client.create("ir.attachment", attachment_values())]
            for call in calls:
                with self.assertRaises(PolicyError):
                    call()
            client._send.assert_not_called()

    def test_attachment_payload_restrictions_are_enforced_in_client(self):
        variants = [{"public": True}, {"res_model": "res.partner"}, {"res_id": 0},
                    {"access_token": "token"}, {"type": "url"}, {"mimetype": "text/html"},
                    {"datas": "invalid"}]
        for change in variants:
            client = make_client()
            with self.subTest(change=list(change)), self.assertRaises(PolicyError):
                client.create("ir.attachment", {**attachment_values(), **change})
            client._send.assert_not_called()
        client = make_client()
        client.create("ir.attachment", attachment_values())
        self.assertEqual(client._send.call_count, 2)

    def test_attachment_metadata_cannot_escape_article_or_read_secrets(self):
        domain = [["res_model", "=", "knowledge.article"], ["res_id", "=", 1], ["type", "=", "binary"]]
        client = make_client()
        for bad_domain, fields in [(domain, ["datas"]), (domain, ["access_token"]), ([], ["id"]),
                                   (["|", *domain], ["id"]), (domain[:2], ["id"])]:
            with self.assertRaises(PolicyError):
                client.search_read("ir.attachment", bad_domain, fields)
        with self.assertRaises(PolicyError):
            client.read("ir.attachment", [1], ["id"])
        client._send.assert_not_called()
        client.search_read("ir.attachment", domain, ["id", "name"])
        self.assertEqual(client._send.call_count, 2)

    def test_general_override_is_not_silently_widened_to_attachments_or_writes(self):
        client = make_client()
        for model, method, args in [("knowledge.article", "write", [[1], {"body": "X"}]),
                                     ("knowledge.article", "unlink", [[1]]), ("ir.attachment", "search", [[]])]:
            with self.assertRaises(PolicyError):
                client.execute_kw(model, method, args)
        client._send.assert_not_called()
        client.execute_kw("knowledge.article", "read", [[1]], {"fields": ["name"]})


class KnowledgeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = fake_client()
        self.client.fields_get.return_value = {name: {} for name in server.ARTICLE_FIELDS + ["body"]}
        self.row = {"id": 1, "name": "Guide", "body": '<p>Keep this</p><img src="/web/image/9"/>',
                    "write_date": REVISION, "active": True, "user_can_write": True}
        self.client.read.return_value = [self.row]
        self.client.create.return_value = 42
        self.client.current_partner_id.return_value = 7
        self.client.image_url.side_effect = lambda rid: f"https://example.com/web/image/{rid}"
        p = patch.object(server, "_get_client", return_value=("test", self.client))
        p.start()
        self.addCleanup(p.stop)

    def upload(self, **overrides):
        args = {"article_id": 1, "filename": "screen.png", "image_base64": screenshot(),
                "expected_write_date": REVISION, "instance": "test"}
        return server.attach_knowledge_image(**(args | overrides))

    def test_search_read_and_scoped_attachment_listing(self):
        self.client.search_read.return_value = [self.row]
        result = server.list_knowledge_articles(query="Guide", parent_id=2, instance="test", limit=1)
        self.assertEqual(result["items"][0]["id"], 1)
        self.assertIn(["parent_id", "=", 2], self.client.search_read.call_args.args[1])
        self.assertNotIn("body", self.client.search_read.call_args.args[2])
        self.assertEqual(server.get_knowledge_article(1, instance="test")["body"], self.row["body"])
        self.client.search_read.return_value = [{"id": 9, "mimetype": "image/png"}]
        result = server.list_knowledge_attachments(1, instance="test")
        self.assertIn(["res_id", "=", 1], self.client.search_read.call_args.args[1])
        self.assertNotIn("datas", self.client.search_read.call_args.args[2])
        self.assertTrue(result["items"][0]["image_url"].endswith("/9"))

    def test_root_is_private_and_child_inherits_without_membership_changes(self):
        server.create_knowledge_article("Guide", body="<p>Text</p>", instance="test")
        values = self.client.create.call_args.args[1]
        self.assertEqual(values["internal_permission"], "none")
        self.assertEqual(values["article_member_ids"], [[0, 0, {"partner_id": 7, "permission": "write"}]])
        server.create_knowledge_article("Child", parent_id=1, instance="test")
        self.assertEqual(self.client.create.call_args.args[1], {"name": "Child", "parent_id": 1})

    def test_append_preserves_existing_html_and_image_references(self):
        result = server.update_knowledge_article(1, REVISION, append_html="<h2>More</h2>", instance="test")
        self.assertEqual(result["status"], "applied")
        self.client.write.assert_called_with("knowledge.article", [1], {"body": self.row["body"] + "<h2>More</h2>"})
        self.client.message_post.assert_called_once()

    def test_missing_stale_or_racing_revision_prevents_write(self):
        for revision in ("", "old"):
            with self.assertRaises(ToolError):
                server.update_knowledge_article(1, revision, body="X", instance="test")
        self.client.read.side_effect = [[self.row], [self.row | {"write_date": "changed"}]]
        with self.assertRaises(ToolError):
            server.update_knowledge_article(1, REVISION, append_html="X", instance="test")
        self.client.write.assert_not_called()

    def test_conflicting_edit_modes_and_nonwritable_articles_rejected(self):
        with self.assertRaises(PolicyError):
            server.update_knowledge_article(1, REVISION, body="X", append_html="Y", instance="test")
        for changes in ({"is_locked": True}, {"active": False}, {"to_delete": True},
                        {"is_template": True}, {"user_can_write": False}):
            self.client.read.return_value = [self.row | changes]
            with self.assertRaises(ToolError):
                self.upload()
        self.client.create.assert_not_called()
        self.client.write.assert_not_called()

    def test_embed_is_nonpublic_and_escapes_alt_text(self):
        result = self.upload(alt_text='" onerror="alert(1)')
        self.assertEqual(result["status"], "applied")
        values = self.client.create.call_args.args[1]
        self.assertIs(values["public"], False)
        self.assertEqual((values["res_model"], values["res_id"]), ("knowledge.article", 1))
        self.assertNotIn("access_token", values)
        body = self.client.write.call_args.args[2]["body"]
        self.assertTrue(body.startswith(self.row["body"]))
        self.assertIn('src="/web/image/42"', body)
        self.assertIn("&quot;", body)

    def test_attach_only_does_not_edit_body_or_post_note(self):
        result = self.upload(embed=False, expected_write_date=None)
        self.assertEqual(result["article_status"], "not_requested")
        self.client.write.assert_not_called()
        self.client.message_post.assert_not_called()

    def test_embed_requires_revision_before_upload(self):
        with self.assertRaises(ToolError):
            self.upload(expected_write_date=None)
        self.client.create.assert_not_called()

    def test_unknown_upload_never_attempts_embedding_or_retry(self):
        self.client.create.side_effect = OdooRPCError("Lost response", outcome="unknown")
        result = self.upload()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["retry_safe"])
        self.client.create.assert_called_once()
        self.client.write.assert_not_called()

    def test_concurrent_edit_after_upload_preserves_attachment_id(self):
        self.client.read.side_effect = [[self.row], [self.row | {"write_date": "changed"}]]
        result = self.upload()
        self.assertEqual((result["status"], result["attachment_id"], result["article_status"]), ("partial", 42, "not_applied"))
        self.client.create.assert_called_once()
        self.client.write.assert_not_called()

    def test_lost_body_response_and_failed_note_report_partial(self):
        self.client.write.side_effect = OdooRPCError("Lost response", outcome="unknown")
        result = self.upload()
        self.assertEqual((result["status"], result["article_status"]), ("partial", "unknown"))
        self.client.message_post.assert_not_called()
        self.client.write.side_effect = None
        self.client.message_post.side_effect = OdooRPCError("Lost note", outcome="unknown")
        result = self.upload()
        self.assertEqual((result["status"], result["article_status"], result["note_status"]), ("partial", "applied", "unknown"))

    def test_unavailable_module_and_auth_failure_are_distinct(self):
        self.client.fields_get.side_effect = OdooRPCError("missing", kind="remote_fault")
        with self.assertRaisesRegex(ToolError, "Knowledge is unavailable"):
            server.get_knowledge_article(1, instance="test")
        self.client.fields_get.side_effect = OdooRPCError("auth", kind="authentication")
        with self.assertRaises(OdooRPCError):
            server.get_knowledge_article(1, instance="test")


class KnowledgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_mutations_fail_read_only_before_any_rpc(self):
        client = make_client(Policy(read_only=True))
        calls = [("create_knowledge_article", {"name": "Guide", "instance": "test"}),
                 ("update_knowledge_article", {"article_id": 1, "expected_write_date": REVISION, "body": "X", "instance": "test"}),
                 ("attach_knowledge_image", {"article_id": 1, "filename": "x.png", "image_base64": screenshot(), "instance": "test"})]
        with patch.object(server, "_get_client", return_value=("test", client)):
            for name, args in calls:
                result = await server.mcp._mcp_server.request_handlers[CallToolRequest](
                    CallToolRequest(method="tools/call", params={"name": name, "arguments": args}))
                self.assertTrue(result.root.isError)
        client._send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
