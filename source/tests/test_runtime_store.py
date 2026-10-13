import sys
import types
import unittest
from unittest.mock import patch

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.OperationalError = RuntimeError
    rows_stub = types.ModuleType("psycopg.rows")
    rows_stub.dict_row = object()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = rows_stub

import main


class RuntimeStoreTest(unittest.TestCase):
    @patch("main.postgres_store.execute_atomic")
    def test_execute_delegates_to_atomic_database_operation(self, execute_atomic):
        execute_atomic.return_value = {"status": "VERIFIED"}
        result = main.store.execute("scope", 0, {"ok": True}, "key-1")
        self.assertEqual(result["status"], "VERIFIED")
        execute_atomic.assert_called_once_with("scope", 0, {"ok": True}, "key-1")

    def test_execute_validates_payload(self):
        with self.assertRaises(ValueError):
            main.store.execute("", 0, {}, "key")
        with self.assertRaises(ValueError):
            main.store.execute("scope", -1, {}, "key")
        with self.assertRaises(ValueError):
            main.store.execute("scope", 0, [], "key")


if __name__ == "__main__":
    unittest.main()
