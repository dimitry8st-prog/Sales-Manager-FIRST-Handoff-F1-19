"""Loopback acceptance tests for the secure API ingress.

The real SecureRuntimeHandler is served over loopback HTTP. Store and database
functions alone are replaced with harmless test doubles; no external service
or production credential is used.
"""

import contextlib
from contextlib import ExitStack
import http.client
import io
import json
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# The project's declared psycopg dependency may not be installed in an offline
# test environment. Supply an import-only guard in that case; a database access
# would fail immediately. Tests patch every route's data dependency below.
try:
    import psycopg  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name != "psycopg":
        raise
    psycopg_shim = types.ModuleType("psycopg")

    class _UnexpectedDatabaseAccess(Exception):
        pass

    def _deny_database_access(*args, **kwargs):
        raise RuntimeError("DATABASE_ACCESS_FORBIDDEN_IN_TEST")

    psycopg_shim.OperationalError = _UnexpectedDatabaseAccess
    psycopg_shim.connect = _deny_database_access
    rows_shim = types.ModuleType("psycopg.rows")
    rows_shim.dict_row = object()
    sys.modules["psycopg"] = psycopg_shim
    sys.modules["psycopg.rows"] = rows_shim

import main
import postgres_store
import sales_store
import secure_entrypoint
import task_store


EXPECTED_KEY = "make-api-key-SENTINEL-75b4f9"
WRONG_KEY = "wrong-api-key-SENTINEL-c028ad"
QUERY_SENTINEL = "query-only-SENTINEL-9df4"
NO_HEADER = object()

# Each entry is an existing application branch. Statuses reflect the current
# handler contract and simple deterministic fake return values, not live data.
GET_CASES = (
    ("GET", "/health", None, 200),
    ("GET", "/ready", "postgres_store.ping", 200),
    ("GET", "/state/ingress-test", "main.store.get_state", 200),
    ("GET", "/audit?limit=2&trace_id=ingress-test", "main.store.get_audit", 200),
    ("GET", "/tasks/task-ingress-test", "task_store.get_task", 200),
    ("GET", "/sales/control", "sales_store.get_control", 200),
    ("GET", "/sales/campaigns/campaign-ingress-test", "sales_store.get_campaign", 200),
    ("GET", "/sales/leads/lead-ingress-test", "sales_store.get_lead", 200),
    ("GET", "/sales/leads/lead-ingress-test/events", "sales_store.list_events", 200),
    ("GET", "/ingress-unknown-route", None, 404),
)

POST_CASES = (
    ("POST", "/tasks", "task_store.create_task", 202),
    ("POST", "/execute", "main.store.execute", 200),
    ("POST", "/sales/campaigns", "sales_store.create_campaign", 201),
    ("POST", "/sales/leads", "sales_store.ingest_lead", 201),
    ("POST", "/sales/control", "sales_store.set_control", 200),
    ("POST", "/sales/campaigns/campaign-ingress-test/status", "sales_store.set_campaign_status", 200),
    ("POST", "/sales/leads/lead-ingress-test/verify", "sales_store.verify_live", 200),
    ("POST", "/sales/leads/lead-ingress-test/audit", "sales_store.save_gold_audit", 200),
    ("POST", "/sales/leads/lead-ingress-test/materials", "sales_store.save_materials", 200),
    ("POST", "/sales/leads/lead-ingress-test/verify-sent", "sales_store.verify_sent", 200),
    ("POST", "/sales/leads/lead-ingress-test/reply", "sales_store.record_reply", 202),
    ("POST", "/sales/leads/lead-ingress-test/reject", "sales_store.reject_lead", 200),
    ("POST", "/ingress-unknown-route", None, 404),
)
ALL_CASES = GET_CASES + POST_CASES

# (target key, object, attribute, safe fake return)
DEPENDENCY_SPECS = (
    ("postgres_store.ping", postgres_store, "ping", {"status": "FAKE_READY"}),
    ("main.store.get_state", main.store, "get_state", {"scope": "fake", "version": 0, "value": {}}),
    ("main.store.get_audit", main.store, "get_audit", []),
    ("main.store.execute", main.store, "execute", {"status": "FAKE_EXECUTED"}),
    ("task_store.get_task", task_store, "get_task", {"task_id": "fake-task"}),
    ("task_store.create_task", task_store, "create_task", {"task_id": "fake-task"}),
    ("sales_store.get_control", sales_store, "get_control", {"emergency_stop": False}),
    ("sales_store.get_campaign", sales_store, "get_campaign", {"campaign_id": "fake-campaign"}),
    ("sales_store.get_lead", sales_store, "get_lead", {"lead_id": "fake-lead"}),
    ("sales_store.list_events", sales_store, "list_events", []),
    ("sales_store.create_campaign", sales_store, "create_campaign", {"campaign_id": "fake-campaign"}),
    ("sales_store.ingest_lead", sales_store, "ingest_lead", {"lead_id": "fake-lead"}),
    ("sales_store.set_control", sales_store, "set_control", {"emergency_stop": False}),
    ("sales_store.set_campaign_status", sales_store, "set_campaign_status", {"status": "FAKE_UPDATED"}),
    ("sales_store.verify_live", sales_store, "verify_live", {"status": "FAKE_VERIFIED"}),
    ("sales_store.save_gold_audit", sales_store, "save_gold_audit", {"status": "FAKE_AUDIT"}),
    ("sales_store.save_materials", sales_store, "save_materials", {"status": "FAKE_MATERIALS"}),
    ("sales_store.verify_sent", sales_store, "verify_sent", {"status": "FAKE_SENT"}),
    ("sales_store.record_reply", sales_store, "record_reply", {"status": "FAKE_REPLY"}),
    ("sales_store.reject_lead", sales_store, "reject_lead", {"status": "FAKE_REJECTED"}),
)

# Retain the genuine implementation before spying on its use.
import secrets as _secrets_module
_REAL_COMPARE_DIGEST = _secrets_module.compare_digest


class SecureIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = secure_entrypoint.ThreadingHTTPServer(
            ("127.0.0.1", 0), secure_entrypoint.SecureRuntimeHandler
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def setUp(self):
        self._env_patcher = patch.dict(os.environ)
        self._env_patcher.start()
        os.environ["MAKE_API_KEY"] = EXPECTED_KEY

        self._patch_stack = ExitStack()
        self.spies = {}
        for name, obj, attribute, return_value in DEPENDENCY_SPECS:
            spy = self._patch_stack.enter_context(
                patch.object(obj, attribute, return_value=return_value)
            )
            self.spies[name] = spy

        self.compare_digest_calls = 0

        def compare_spy(left, right):
            # Record only that the comparison happened; never store/print values.
            self.compare_digest_calls += 1
            return _REAL_COMPARE_DIGEST(left, right)

        self._patch_stack.enter_context(
            patch("secure_entrypoint.secrets.compare_digest", side_effect=compare_spy)
        )
        self.logs = io.StringIO()

    def tearDown(self):
        self._patch_stack.close()
        self._env_patcher.stop()

    def _reset_spies(self):
        for spy in self.spies.values():
            spy.reset_mock()

    def _assert_no_dependency_calls(self):
        called = [name for name, spy in self.spies.items() if spy.called]
        self.assertEqual(called, [], f"Unauthenticated/unsupported request reached dependencies: {called}")

    def _request(self, method, path, api_key=NO_HEADER, body=None):
        headers = {}
        if api_key is not NO_HEADER:
            headers["X-API-Key"] = api_key
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        try:
            # BaseHTTPRequestHandler handles requests on another thread. Its
            # access logger runs before the response is sent, so this captures
            # the real loopback handler's log output for leakage assertions.
            with contextlib.redirect_stdout(self.logs):
                connection.request(method, path, body=encoded_body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                status = response.status
            return status, payload
        finally:
            connection.close()

    def _assert_no_sentinels(self, payload):
        text = payload.decode("utf-8", errors="replace")
        for sentinel in (EXPECTED_KEY, WRONG_KEY, QUERY_SENTINEL):
            self.assertNotIn(sentinel, text)

    def test_every_existing_get_post_branch_requires_key_and_delegates_with_correct_key(self):
        self.assertIs(self.server.RequestHandlerClass, secure_entrypoint.SecureRuntimeHandler)
        variants = (
            ("missing", NO_HEADER),
            ("empty", ""),
            ("wrong", WRONG_KEY),
            ("correct", EXPECTED_KEY),
        )
        for method, path, expected_target, expected_status in ALL_CASES:
            for label, key in variants:
                with self.subTest(method=method, path=path, key_variant=label):
                    self._reset_spies()
                    status, payload = self._request(method, path, key, body={} if method == "POST" else None)
                    self._assert_no_sentinels(payload)
                    if label != "correct":
                        self.assertEqual(status, 401)
                        self.assertIn(b'"error": "UNAUTHORIZED"', payload)
                        self._assert_no_dependency_calls()
                    else:
                        self.assertEqual(status, expected_status)
                        if expected_target is None:
                            # /health has no backing-store dependency, but a
                            # valid key reaches the actual parent route (200).
                            self._assert_no_dependency_calls()
                        else:
                            self.spies[expected_target].assert_called_once()
                            self.assertTrue(self.spies[expected_target].called)
        self.assertGreater(self.compare_digest_calls, 0, "Expected keys must be checked using secrets.compare_digest")

    def test_unset_or_empty_configured_key_fails_closed_for_every_get_post_branch(self):
        for env_mode in ("unset", "empty"):
            for method, path, _expected_target, _expected_status in ALL_CASES:
                with self.subTest(env_mode=env_mode, method=method, path=path):
                    self._reset_spies()
                    with patch.dict(os.environ):
                        if env_mode == "unset":
                            os.environ.pop("MAKE_API_KEY", None)
                        else:
                            os.environ["MAKE_API_KEY"] = ""
                        status, payload = self._request(method, path, EXPECTED_KEY, body={} if method == "POST" else None)
                    self.assertEqual(status, 401)
                    self._assert_no_sentinels(payload)
                    self._assert_no_dependency_calls()

    def test_path_slash_and_query_parameters_do_not_bypass_authentication(self):
        variants = (
            ("GET", f"/health?X-API-Key={EXPECTED_KEY}"),
            ("GET", f"/health/?X-API-Key={EXPECTED_KEY}"),
            ("POST", f"/sales/leads/lead-ingress-test/reply/?X-API-Key={EXPECTED_KEY}"),
        )
        for method, path in variants:
            with self.subTest(method=method, path="query/path variant"):
                self._reset_spies()
                status, payload = self._request(method, path, NO_HEADER, body={} if method == "POST" else None)
                self.assertEqual(status, 401)
                self._assert_no_sentinels(payload)
                self._assert_no_dependency_calls()

        # Valid header access with a secret-looking query value remains valid
        # for the exact route, but the query must not be written to access logs.
        status, payload = self._request("GET", f"/health?trace={QUERY_SENTINEL}", EXPECTED_KEY)
        self.assertEqual(status, 200)
        self._assert_no_sentinels(payload)

    def test_unicode_wrong_key_is_rejected_with_401_not_a_handler_error(self):
        status, payload = self._request("GET", "/health", "caf\u00e9-wrong-key")
        self.assertEqual(status, 401)
        self._assert_no_sentinels(payload)
        self._assert_no_dependency_calls()

    def test_unsupported_methods_never_reach_stores(self):
        for method in ("HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                self._reset_spies()
                status, payload = self._request(method, "/sales/control", EXPECTED_KEY, body={})
                self.assertEqual(status, 501)
                if method == "HEAD":
                    self.assertEqual(payload, b"")
                self._assert_no_dependency_calls()
                self._assert_no_sentinels(payload)

    def test_run_server_does_not_abort_when_make_api_key_is_missing(self):
        fake_server = MagicMock()
        with patch.dict(os.environ):
            os.environ.pop("MAKE_API_KEY", None)
            os.environ["PORT"] = "18080"
            with patch("secure_entrypoint.ThreadingHTTPServer", return_value=fake_server) as server_factory:
                with contextlib.redirect_stdout(io.StringIO()):
                    secure_entrypoint.run_server()
        server_factory.assert_called_once_with(("0.0.0.0", 18080), secure_entrypoint.SecureRuntimeHandler)
        fake_server.serve_forever.assert_called_once_with()

    def test_dockerfile_uses_secure_entrypoint_as_default(self):
        with open("Dockerfile", encoding="utf-8") as source_file:
            dockerfile = source_file.read()
        commands = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("CMD ")]
        self.assertEqual(commands, ['CMD ["python", "secure_entrypoint.py"]'])

    def test_keys_are_not_written_to_responses_or_logs(self):
        # Put the key values in headers and in a query string deliberately.
        for method, path, _target, _status in ALL_CASES:
            self._reset_spies()
            self._request(method, path, WRONG_KEY, body={} if method == "POST" else None)
        self._request("GET", f"/health?credential={EXPECTED_KEY}", NO_HEADER)
        self._request("GET", f"/health?trace={QUERY_SENTINEL}", EXPECTED_KEY)
        log_text = self.logs.getvalue()
        for sentinel in (EXPECTED_KEY, WRONG_KEY, QUERY_SENTINEL):
            self.assertNotIn(sentinel, log_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
