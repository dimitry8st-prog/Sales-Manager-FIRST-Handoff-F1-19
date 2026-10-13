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

import sales_manager
import sales_store


class SalesManagerTest(unittest.TestCase):
    @patch("sales_manager.sales_store.get_control", return_value={"emergency_stop": False})
    @patch("sales_manager.sales_store.save_materials")
    @patch("sales_manager.sales_store.save_gold_audit")
    @patch("sales_manager.sales_store.verify_live")
    @patch("sales_manager.connectors.post_json")
    @patch("sales_manager.sales_store.get_campaign")
    @patch("sales_manager.sales_store.get_lead")
    def test_prepare_runs_all_gates_and_auto_queues_send(
        self, get_lead, get_campaign, post_json, verify_live, save_audit,
        save_materials, _control
    ):
        get_lead.side_effect = [
            {"lead_id": "lead-1", "campaign_id": "campaign-1", "stage": "DUPLICATE_CLEARED"},
            {"lead_id": "lead-1", "campaign_id": "campaign-1", "stage": "DUPLICATE_CLEARED"},
        ]
        get_campaign.return_value = {"campaign_id": "campaign-1", "config": {"mode": "AUTO"}}
        post_json.return_value = {
            "live_evidence": {"primary_source_url": "https://example.com", "observed_at": "2026-08-29T12:00:00Z"},
            "gold_audit": {"market_signal": "a", "deep_audit": "b", "needs_mapping": "c", "focused_thesis": "d"},
            "materials": {"subject": "s", "body": "b", "attachments": []},
        }
        verify_live.return_value = {"stage": "LIVE_VERIFIED"}
        save_audit.return_value = {"stage": "GOLD_AUDITED"}
        save_materials.return_value = {"stage": "SEND_QUEUED"}
        result = sales_manager.process_prepare({
            "scope": "sales.prepare", "payload": {"lead_id": "lead-1"}
        })
        self.assertEqual(result["lead"]["stage"], "SEND_QUEUED")
        verify_live.assert_called_once()
        save_audit.assert_called_once()
        save_materials.assert_called_once()

    @patch("sales_manager.sales_store.get_control", return_value={"emergency_stop": False})
    @patch("sales_manager.sales_store.ingest_lead")
    @patch("sales_manager.connectors.post_json")
    @patch("sales_manager.sales_store.get_active_lead", return_value=None)
    @patch("sales_manager.sales_store.get_campaign")
    def test_discovery_silently_skips_duplicate(
        self, get_campaign, _active, post_json, ingest, _control
    ):
        get_campaign.return_value = {"campaign_id": "campaign-1", "status": "RUNNING"}
        post_json.side_effect = [
            {"status": "FOUND", "lead": {"company_name": "Duplicate"}},
            {"status": "FOUND", "lead": {"company_name": "New"}},
        ]
        ingest.side_effect = [
            sales_store.DuplicateLead("DUPLICATE"),
            {"lead_id": "lead-2", "stage": "DUPLICATE_CLEARED"},
        ]
        result = sales_manager.process_discover({
            "scope": "sales.discover",
            "attempts": 1,
            "dedupe_key": "discover-1",
            "payload": {"campaign_id": "campaign-1"},
        })
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(result["lead"]["lead_id"], "lead-2")

    @patch("sales_manager.sales_store.record_send")
    @patch("sales_manager.connectors.post_json")
    @patch("sales_manager.sales_store.reserve_send_slot")
    @patch("sales_manager.sales_store.get_lead")
    def test_send_queued_lead_sends_without_user_command(
        self, get_lead, reserve, post_json, record_send
    ):
        lead = {
            "lead_id": "lead-1",
            "stage": "SEND_QUEUED",
            "company_name": "Example",
            "materials": {"subject": "Hello", "body": "Body"},
        }
        get_lead.return_value = lead
        reserve.return_value = lead
        post_json.return_value = {"accepted": True, "sent_verified": True}
        record_send.return_value = {**lead, "stage": "SENT_VERIFIED"}
        result = sales_manager.process_send({
            "scope": "sales.send", "payload": {"lead_id": "lead-1"}
        })
        self.assertEqual(result["lead"]["stage"], "SENT_VERIFIED")
        post_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
