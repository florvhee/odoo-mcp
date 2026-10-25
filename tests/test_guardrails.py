import http.client
import unittest
import xmlrpc.client
from unittest.mock import Mock, patch
from xml.parsers.expat import ExpatError

from odoo_client import OdooClient, OdooInstance, OdooRPCError, _HTTPTransport, _HTTPSTransport
from policy import Policy, PolicyError


def make_client(policy=None, explicit=True):
    client = OdooClient(OdooInstance("test", "https://odoo.example", "testdb", "mcp", "SECRET"), policy, explicit_instance=explicit)
    client._send = Mock(return_value=7)
    return client


class PolicyTests(unittest.TestCase):
    def test_read_only_blocks_every_write_route_before_network(self):
        client = make_client(Policy(read_only=True, general_mode="full", general_models=("*",)))
        operations = [
            lambda: client.create("project.project", {"name": "X"}),
            lambda: client.create("project.task", {"name": "X"}),
            lambda: client.write("helpdesk.ticket", [1], {"name": "X"}),
            lambda: client.message_post("project.task", 1, "note"),
            lambda: client.execute_kw("res.users", "unlink", [[1]]),
            lambda: client.execute_kw("project.task", "action_custom", [[1]]),
            lambda: client.execute_kw("project.task", "write", [[1], {"name": "X"}], {"context": {"sudo": True}}),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(PolicyError):
                operation()
        client._send.assert_not_called()

    def test_default_override_rejects_writes_and_unlisted_models(self):
        client = make_client()
        for model, method, args in [("project.task", "write", [[1], {"name": "X"}]),
                                    ("account.move", "search", [[]]),
                                    ("project.task", "action_done", [[1]])]:
            with self.subTest(model=model, method=method), self.assertRaises(PolicyError):
                client.execute_kw(model, method, args)
        client._send.assert_not_called()

    def test_disabled_override_does_not_disable_dedicated_tools(self):
        client = make_client(Policy(general_mode="disabled"))
        with self.assertRaises(PolicyError):
            client.execute_kw("project.task", "read", [[1]], {"fields": ["id"]})
        client._send.assert_not_called()
        client.create("project.task", {"name": "X", "project_id": 1})
        self.assertEqual(client._send.call_count, 2)

    def test_full_override_explicitly_supports_arbitrary_public_methods(self):
        client = make_client(Policy(general_mode="full", general_models=("*",)))
        self.assertEqual(client.execute_kw("custom.model", "action_work", [[1]]), 7)
        client._send.assert_called_with("object", "execute_kw", "testdb", 7, "SECRET", "custom.model", "action_work", [[1]], {})

    def test_full_override_still_enforces_model_allowlist(self):
        client = make_client(Policy(general_mode="full", general_models=("project.task",)))
        with self.assertRaises(PolicyError):
            client.execute_kw("res.users", "unlink", [[1]])
        client._send.assert_not_called()

    def test_mutations_require_explicit_instance_including_override(self):
        client = make_client(Policy(general_mode="full"), explicit=False)
        for operation in (lambda: client.create("project.task", {"name": "X"}),
                          lambda: client.execute_kw("project.task", "write", [[1], {"name": "X"}])):
            with self.assertRaises(PolicyError):
                operation()
        client._send.assert_not_called()
        client.read("project.task", [1], ["id"])

    def test_explicit_instance_requirement_can_be_disabled_in_config(self):
        client = make_client(Policy(require_explicit_instance=False), explicit=False)
        client.create("project.task", {"name": "X"})
        self.assertEqual(client._send.call_count, 2)

    def test_private_methods_rejected_even_in_full_mode(self):
        client = make_client(Policy(general_mode="full", general_models=("*",)))
        with self.assertRaises(PolicyError):
            client.execute_kw("project.task", "_private", [])
        client._send.assert_not_called()

    def test_limit_validation_blocks_positional_and_keyword_bypasses(self):
        for limit in (0, -1, 201, False, 1.5, None):
            for method, args, kwargs in (
                ("search", [[], 0, limit], {}),
                ("search_read", [[], ["id"], 0, limit], {}),
                ("search_read", [[]], {"fields": ["id"], "limit": limit}),
            ):
                with self.subTest(method=method, limit=limit):
                    client = make_client()
                    with self.assertRaises(PolicyError):
                        client.execute_kw("project.task", method, args, kwargs)
                    client._send.assert_not_called()

    def test_duplicate_arguments_cannot_override_checked_values(self):
        client = make_client()
        with self.assertRaises(PolicyError):
            client.execute_kw("project.task", "search", [[], 0, 10], {"limit": 0})
        client._send.assert_not_called()

    def test_default_read_limit_and_order_are_inserted(self):
        client = make_client()
        client.execute_kw("project.task", "search_read", [[]], {"fields": ["id"]})
        payload = client._send.call_args.args[-1]
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["order"], "id asc")
        self.assertEqual(payload["offset"], 0)

    def test_fields_ids_offsets_and_batch_sizes_are_validated(self):
        calls = [
            ("read", [[1]], {}), ("read", [[1]], {"fields": []}),
            ("read", [[True]], {"fields": ["id"]}), ("read", [[0]], {"fields": ["id"]}),
            ("read", [list(range(1, 202))], {"fields": ["id"]}),
            ("search", [[]], {"offset": -1}),
            ("write", [list(range(1, 202)), {"name": "X"}], {}),
            ("create", [[{"name": "X"}] * 201], {}),
        ]
        for method, args, kwargs in calls:
            client = make_client(Policy(general_mode="full"))
            with self.subTest(method=method), self.assertRaises(PolicyError):
                client.execute_kw("project.task", method, args, kwargs)
            client._send.assert_not_called()

    def test_config_merges_per_instance_and_fails_closed_on_typos(self):
        policy = Policy.from_config({"read_only": True, "general_mode": "full"}, {"general_mode": "disabled"})
        self.assertTrue(policy.read_only)
        self.assertEqual(policy.general_mode, "disabled")
        for bad in ({"read_only": "false"}, {"general_mode": "ful"}, {"max_records": 0},
                    {"timeout_seconds": 301}, {"general_models": "*"}, {"readonly": False}):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                Policy.from_config(bad)

    def test_core_path_cannot_access_unrelated_model(self):
        client = make_client(Policy(general_mode="full", general_models=("*",)))
        with self.assertRaises(PolicyError):
            client.write("account.move", [1], {"state": "posted"})
        client._send.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_disconnected_requests_are_never_replayed(self):
        for transport_class in (_HTTPTransport, _HTTPSTransport):
            transport = transport_class(10)
            transport.single_request = Mock(side_effect=http.client.RemoteDisconnected())
            with self.assertRaises(http.client.RemoteDisconnected):
                transport.request("example.com", "/xmlrpc/2/object", b"body")
            self.assertEqual(transport.single_request.call_count, 1)
            self.assertEqual(transport.make_connection("example.com").timeout, 10)

    def test_mutation_transport_faults_are_unknown_and_sanitized(self):
        for error in (TimeoutError("SECRET"), xmlrpc.client.Fault(1, "SECRET traceback"), ExpatError("SECRET")):
            client = make_client()
            client._uid = 7
            client._send.side_effect = error
            with self.subTest(error=error), self.assertRaises(OdooRPCError) as caught:
                client.create("project.task", {"name": "X"})
            self.assertEqual(caught.exception.outcome, "unknown")
            self.assertNotIn("SECRET", str(caught.exception))
            client._send.assert_called_once()

    def test_auth_failure_never_sends_mutation(self):
        client = make_client()
        client._send.side_effect = TimeoutError("SECRET")
        with self.assertRaises(OdooRPCError) as caught:
            client.create("project.task", {"name": "X"})
        self.assertEqual(caught.exception.outcome, "not_applied")
        self.assertEqual(client._send.call_count, 1)
        self.assertEqual(client._send.call_args.args[0], "common")

    def test_secrets_are_not_in_instance_repr(self):
        self.assertNotIn("SECRET", repr(make_client()._inst))

    def test_non_encodable_values_fail_before_network(self):
        client = make_client()
        with self.assertRaises(ValueError):
            client.create("project.task", {"name": None})
        client._send.assert_not_called()

    def test_existing_internal_note_semantics_preserved(self):
        client = make_client()
        client.message_post("project.task", 4, "My note")
        self.assertEqual(client._send.call_args.args[-1], {
            "body": "My note", "message_type": "comment", "subtype_xmlid": "mail.mt_note",
        })


if __name__ == "__main__":
    unittest.main()
