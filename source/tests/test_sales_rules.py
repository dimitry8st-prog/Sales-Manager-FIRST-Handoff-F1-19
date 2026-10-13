import unittest

import sales_rules


class SalesRulesTest(unittest.TestCase):
    def test_stage_machine_allows_only_next_stage(self):
        sales_rules.assert_transition("MATERIALS_READY", "SEND_QUEUED")
        with self.assertRaises(sales_rules.RuleViolation):
            sales_rules.assert_transition("MATERIALS_READY", "SENT")

    def test_autonomous_policy_is_explicit(self):
        sales_rules.validate_auto_policy({"mode": "AUTO"})
        with self.assertRaises(sales_rules.RuleViolation):
            sales_rules.validate_auto_policy({"mode": "MANUAL"})

    def test_ingest_requires_primary_source_and_contact_route(self):
        lead = {
            "campaign_id": "13cf60c1-43df-4bda-a53d-a2aa33d1f585",
            "company_name": "Example Ltd",
            "source_url": "https://example.com/jobs/1",
            "source_observed_at": "2026-08-29T12:00:00Z",
        }
        with self.assertRaises(sales_rules.RuleViolation):
            sales_rules.validate_ingest(lead)
        lead["email"] = "owner@example.com"
        sales_rules.validate_ingest(lead)

    def test_fingerprints_normalize_company_domain_email_and_phone(self):
        result = sales_rules.fingerprints({
            "company_name": "  Example, LTD ",
            "domain": "https://www.Example.com/path",
            "email": " Owner@Example.com ",
            "phone": "+66 (81) 234-5678",
            "aliases": ["Example Group"],
        })
        self.assertIn(("company", "example ltd"), result)
        self.assertIn(("domain", "example.com"), result)
        self.assertIn(("email", "owner@example.com"), result)
        self.assertIn(("phone", "66812345678"), result)
        self.assertIn(("alias", "example group"), result)

    def test_live_verification_requires_email_evidence(self):
        with self.assertRaises(sales_rules.RuleViolation):
            sales_rules.validate_live_evidence(
                {
                    "primary_source_url": "https://example.com/jobs/1",
                    "observed_at": "2026-08-29T12:00:00Z",
                    "source_status": "LIVE",
                    "freshness_verified": True,
                    "email_status": "VERIFIED",
                },
                {"email": "owner@example.com"},
            )

    def test_gold_audit_requires_all_four_sections(self):
        with self.assertRaises(sales_rules.RuleViolation):
            sales_rules.validate_gold_audit({"market_signal": "signal"})
        sales_rules.validate_gold_audit({
            "market_signal": "signal",
            "deep_audit": "audit",
            "needs_mapping": "needs",
            "focused_thesis": "thesis",
        })


if __name__ == "__main__":
    unittest.main()
