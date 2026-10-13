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

import worker


class WorkerTest(unittest.TestCase):
    @patch("worker.task_store.claim_next_task", return_value=None)
    def test_idle(self, _claim):
        self.assertEqual(worker.run_once(), {"status": "IDLE"})

    @patch("worker.task_store.complete_task")
    @patch("worker.sales_manager.process_task")
    @patch("worker.task_store.claim_next_task")
    def test_routes_sales_task(self, claim, process, complete):
        claim.return_value = {
            "task_id": "task-1",
            "scope": "sales.send",
            "payload": {"lead_id": "lead-1"},
        }
        process.return_value = {"operation": "send"}
        complete.return_value = {"task_id": "task-1", "status": "SUCCEEDED"}
        result = worker.run_once()
        self.assertEqual(result["status"], "DONE")
        process.assert_called_once()
        complete.assert_called_once_with("task-1", {"operation": "send"})

    @patch("worker.task_store.fail_task")
    @patch("worker.sales_manager.process_task", side_effect=RuntimeError("boom"))
    @patch("worker.task_store.claim_next_task")
    def test_failure_returns_task_to_retry_policy(self, claim, _process, fail):
        claim.return_value = {
            "task_id": "task-2",
            "scope": "sales.send",
            "payload": {"lead_id": "lead-2"},
        }
        fail.return_value = {"task_id": "task-2", "status": "PENDING"}
        result = worker.run_once()
        self.assertEqual(result["status"], "ERROR")
        fail.assert_called_once_with("task-2", "boom")

    @patch("worker.task_store.defer_task")
    @patch("worker.sales_manager.process_task")
    @patch("worker.task_store.claim_next_task")
    def test_control_block_defers_without_consuming_retry(self, claim, process, defer):
        claim.return_value = {
            "task_id": "task-3",
            "scope": "sales.discover",
            "payload": {"campaign_id": "campaign-1"},
        }
        process.side_effect = worker.sales_store.ControlBlocked("EMERGENCY_STOP_ACTIVE")
        defer.return_value = {"task_id": "task-3", "status": "PENDING"}
        result = worker.run_once()
        self.assertEqual(result["status"], "DEFERRED")
        defer.assert_called_once_with("task-3", "EMERGENCY_STOP_ACTIVE", delay_seconds=300)

    @patch("worker.task_store.create_task")
    @patch("worker.task_store.fail_task")
    @patch("worker.sales_manager.process_task", side_effect=RuntimeError("final"))
    @patch("worker.task_store.claim_next_task")
    def test_final_failure_queues_operational_alert(
        self, claim, _process, fail, create_task
    ):
        claim.return_value = {
            "task_id": "task-4",
            "scope": "sales.prepare",
            "payload": {"lead_id": "lead-4"},
        }
        fail.return_value = {
            "task_id": "task-4",
            "status": "FAILED",
            "attempts": 8,
            "last_error": "final",
        }
        result = worker.run_once()
        self.assertTrue(result["alert_queued"])
        create_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
