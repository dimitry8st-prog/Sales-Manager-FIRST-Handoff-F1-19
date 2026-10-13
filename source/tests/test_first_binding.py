import builtins
import copy
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sales_first_binding


NOW = datetime.fromisoformat("2026-10-12T10:00:00+00:00")
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "first_binding"


def load_fixture(name):
    with (FIXTURE_DIR / name).open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def valid_config():
    return load_fixture("positive_en_ru.json")


class FirstBindingTest(unittest.TestCase):
    def assert_code(self, config, expected, *, now=NOW):
        with self.assertRaises(sales_first_binding.FirstBindingError) as caught:
            sales_first_binding.validate_first_binding(config, now=now)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)

    def test_positive_example_normalizes_expected_fields(self):
        config = valid_config()
        result = sales_first_binding.validate_first_binding(config, now=NOW)

        self.assertEqual(result["schema_version"], "FIRST_BINDING_V1")
        self.assertEqual(result["mailbox"]["sender_email"], "seller@example.test")
        self.assertEqual(result["approved_fact_ids"], ["fact-test"])
        self.assertEqual(result["release_mode"], "NO_OUTBOUND")
        self.assertEqual(result["offer"]["name"], "Synthetic offer")
        self.assertEqual(result["allowed_locales"], ["en-GB", "ru-RU"])

    def test_input_schema_has_seventeen_required_fields(self):
        self.assertEqual(len(valid_config()), 17)
        result = sales_first_binding.validate_first_binding(valid_config(), now=NOW)
        self.assertEqual(len(result), 18)

    def test_accepts_en_ru_and_additional_synthetic_locale(self):
        config = valid_config()
        config["allowed_locales"].append("de-DE")
        config["language_pack_versions"]["de-DE"] = "de-v1"

        result = sales_first_binding.validate_first_binding(config, now=NOW)

        self.assertEqual(result["allowed_locales"], ["en-GB", "ru-RU", "de-DE"])
        self.assertEqual(result["language_pack_versions"]["de-DE"], "de-v1")

    def test_runtime_and_external_campaign_ids_remain_distinct(self):
        config = valid_config()
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(result["runtime_campaign_id"], config["runtime_campaign_id"])
        self.assertEqual(result["make_campaign_id"], "make-campaign-test")

        config["runtime_campaign_id"] = "make-campaign-test"
        self.assert_code(config, "INVALID_IDENTITY")

    def test_only_canonical_lowercase_runtime_uuid_is_accepted(self):
        for uuid in (
            "11111111-1111-4111-8111-11111111111A",
            "{11111111-1111-4111-8111-111111111111}",
            "11111111111141118111111111111111",
        ):
            with self.subTest(uuid=uuid):
                config = valid_config()
                config["runtime_campaign_id"] = uuid
                self.assert_code(config, "INVALID_IDENTITY")

    def test_identifiers_use_full_ascii_match(self):
        for value in (" leading", "trailing ", "has space", "ümlaut", "line\n"):
            with self.subTest(value=value):
                config = valid_config()
                config["client_id"] = value
                self.assert_code(config, "INVALID_IDENTITY")

    def test_missing_and_unknown_top_level_fields_are_schema_errors(self):
        missing = valid_config()
        del missing["make_campaign_id"]
        self.assert_code(missing, "INVALID_SCHEMA")

        unknown = valid_config()
        unknown["grant"] = True
        self.assert_code(unknown, "INVALID_SCHEMA")

        wrong_version = valid_config()
        wrong_version["schema_version"] = "OTHER"
        self.assert_code(wrong_version, "INVALID_SCHEMA")

    def test_tz_negative_json_examples_have_one_expected_error_each(self):
        cases = (
            (
                "negative_outbound_mode.json",
                "OUTBOUND_MODE_FORBIDDEN",
                lambda c: c.update(release_mode="LIVE"),
            ),
            (
                "negative_bool_limit.json",
                "INVALID_LIMITS",
                lambda c: c.update(daily_send_limit=True),
            ),
            (
                "negative_language_keyset.json",
                "INVALID_LANGUAGE_CONFIG",
                lambda c: c["language_pack_versions"].pop("ru-RU"),
            ),
            (
                "negative_email.json",
                "INVALID_EMAIL",
                lambda c: c["mailbox"].update(sender_email="   "),
            ),
            (
                "negative_expired.json",
                "INVALID_VALIDITY",
                lambda c: c.update(expires_at="2026-10-12T10:00:00+00:00"),
            ),
            (
                "negative_runtime_id.json",
                "INVALID_IDENTITY",
                lambda c: c.update(runtime_campaign_id="make-campaign-test"),
            ),
            (
                "negative_duplicate_fact.json",
                "INVALID_FACTS",
                lambda c: c["approved_facts"].append(
                    copy.deepcopy(c["approved_facts"][0])
                ),
            ),
            (
                "negative_unknown_field.json",
                "INVALID_SCHEMA",
                lambda c: c.update(grant=True),
            ),
        )
        positive = valid_config()
        for name, expected_code, make_expected in cases:
            with self.subTest(fixture=name):
                expected_config = copy.deepcopy(positive)
                make_expected(expected_config)
                fixture_config = load_fixture(name)
                self.assertEqual(fixture_config, expected_config)
                self.assert_code(fixture_config, expected_code)

    def test_invalid_argument_types_and_naive_now_are_input_errors(self):
        for config in (None, [], "config"):
            with self.subTest(config_type=type(config).__name__):
                self.assert_code(config, "INVALID_INPUT")
        self.assert_code(valid_config(), "INVALID_INPUT", now="2026-10-12T10:00:00Z")
        self.assert_code(
            valid_config(),
            "INVALID_INPUT",
            now=datetime.fromisoformat("2026-10-12T10:00:00"),
        )

        class DateTimeSubclass(datetime):
            pass

        subclass_now = DateTimeSubclass(2026, 10, 12, 10, tzinfo=timezone.utc)
        subclass_result = sales_first_binding.validate_first_binding(
            valid_config(), now=subclass_now
        )
        self.assertEqual(subclass_result["schema_version"], "FIRST_BINDING_V1")

    def test_now_utc_conversion_overflow_is_an_input_error(self):
        extreme_now = datetime.min.replace(tzinfo=timezone(timedelta(hours=23)))
        self.assert_code(valid_config(), "INVALID_INPUT", now=extreme_now)

    def test_offer_shape_is_checked_at_offer_group(self):
        for offer in (None, {}, {"offer_id": "offer-test", "name": "n", "description": "d", "extra": 1}):
            with self.subTest(offer=offer):
                config = valid_config()
                config["offer"] = offer
                self.assert_code(config, "INVALID_SCHEMA")

    def test_offer_text_must_be_nonempty_and_is_preserved(self):
        for field in ("name", "description"):
            config = valid_config()
            config["offer"][field] = " \t\n "
            self.assert_code(config, "INVALID_OFFER")

        result = sales_first_binding.validate_first_binding(valid_config(), now=NOW)
        self.assertEqual(result["offer"]["name"], "Synthetic offer")

    def test_mailbox_shape_is_checked_at_email_group(self):
        config = valid_config()
        config["mailbox"] = {"mailbox_ref": "mb-test"}
        self.assert_code(config, "INVALID_SCHEMA")

    def test_email_is_trimmed_lowercased_and_syntax_checked(self):
        config = valid_config()
        config["mailbox"]["sender_email"] = "  Seller+Tag@Example.Test  "
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(result["mailbox"]["sender_email"], "seller+tag@example.test")

        for value in (None, "", "  ", "missing-at.example.test", "a@b"):
            with self.subTest(value=value):
                config = valid_config()
                config["mailbox"]["sender_email"] = value
                self.assert_code(config, "INVALID_EMAIL")

    def test_approval_reference_must_be_nonempty_text(self):
        for value in (None, "", " \t ", 7):
            with self.subTest(value=value):
                config = valid_config()
                config["owner_approval_ref"] = value
                self.assert_code(config, "INVALID_APPROVAL_REF")

    def test_validity_normalizes_offsets_to_utc(self):
        config = valid_config()
        config["valid_from"] = "2026-10-12T12:00:00+03:00"
        config["expires_at"] = "2026-10-13T12:00:00+03:00"

        result = sales_first_binding.validate_first_binding(config, now=NOW)

        self.assertEqual(result["valid_from"], "2026-10-12T09:00:00+00:00")
        self.assertEqual(result["expires_at"], "2026-10-13T09:00:00+00:00")

    def test_utc_z_suffix_is_supported(self):
        config = valid_config()
        config["valid_from"] = "2026-10-12T09:00:00Z"
        config["expires_at"] = "2026-10-13T09:00:00Z"
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(result["valid_from"], "2026-10-12T09:00:00+00:00")

    def test_validity_includes_start_and_excludes_expiry(self):
        config = valid_config()
        start = datetime.fromisoformat(config["valid_from"])
        end = datetime.fromisoformat(config["expires_at"])
        sales_first_binding.validate_first_binding(config, now=start)
        self.assert_code(config, "INVALID_VALIDITY", now=end)
        self.assert_code(config, "INVALID_VALIDITY", now=start - timedelta(seconds=1))

    def test_invalid_or_reversed_validity_is_rejected(self):
        for start, end in (
            ("not-a-date", "2026-10-13T09:00:00+00:00"),
            ("2026-10-12T09:00:00", "2026-10-13T09:00:00+00:00"),
            ("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:00+00:00"),
            ("2026-10-12T11:00:00+00:00", "2026-10-12T10:00:00+00:00"),
            ("2026-10-12T09:00:00z", "2026-10-13T09:00:00+00:00"),
        ):
            with self.subTest(start=start, end=end):
                config = valid_config()
                config["valid_from"] = start
                config["expires_at"] = end
                self.assert_code(config, "INVALID_VALIDITY")

    def test_timestamp_utc_conversion_overflow_is_a_validity_error(self):
        config = valid_config()
        config["valid_from"] = "0001-01-01T00:00:00+23:00"
        self.assert_code(config, "INVALID_VALIDITY")

    def test_outbound_mode_is_always_forbidden(self):
        config = valid_config()
        config["release_mode"] = "LIVE"
        self.assert_code(config, "OUTBOUND_MODE_FORBIDDEN")

    def test_locale_list_is_extensible_unique_and_case_sensitive(self):
        config = valid_config()
        config["allowed_locales"] = ["en-GB", "ru-RU", "de-DE"]
        config["language_pack_versions"]["de-DE"] = "de-v1"
        config["default_locale"] = "de-DE"
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(result["default_locale"], "de-DE")

        config["default_locale"] = "DE-de"
        self.assert_code(config, "INVALID_LANGUAGE_CONFIG")

    def test_invalid_duplicate_or_empty_locale_config_is_rejected(self):
        cases = (
            ([], "en-GB", {"en-GB": "en-v1", "ru-RU": "ru-v1"}),
            (["en-GB", "en-GB"], "en-GB", {"en-GB": "en-v1"}),
            (["en-GB", "bad_locale"], "en-GB", {"en-GB": "en-v1", "bad_locale": "x"}),
            (["en-GB", "ru-RU"], "fr-FR", {"en-GB": "en-v1", "ru-RU": "ru-v1"}),
            (["en-GB", "ru-RU"], "en-GB", {"en-GB": "en-v1"}),
            (["en-GB"], "en-GB", {"en-GB": "version with spaces"}),
            (["en-GB"], "en-GB", ["not", "a", "dict"]),
        )
        for locales, default, versions in cases:
            with self.subTest(locales=locales, default=default, versions=versions):
                config = valid_config()
                config["allowed_locales"] = locales
                config["default_locale"] = default
                config["language_pack_versions"] = versions
                self.assert_code(config, "INVALID_LANGUAGE_CONFIG")

    def test_limits_require_integers_and_reject_bool(self):
        for field, value in (
            ("daily_send_limit", True),
            ("daily_send_limit", 0),
            ("daily_send_limit", 1.5),
            ("max_followups", False),
            ("max_followups", -1),
        ):
            with self.subTest(field=field, value=value):
                config = valid_config()
                config[field] = value
                self.assert_code(config, "INVALID_LIMITS")

    def test_fact_shape_text_order_uniqueness_and_ids(self):
        config = valid_config()
        config["approved_facts"] = [
            {"fact_id": "first", "text": "  Unicode: café 東京  ", "source_ref": " ref-1 "},
            {"fact_id": "second", "text": "Second fact", "source_ref": "ref-2"},
        ]
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(result["approved_fact_ids"], ["first", "second"])
        self.assertEqual(result["approved_facts"][0]["text"], "  Unicode: café 東京  ")
        self.assertEqual(result["approved_facts"][0]["source_ref"], " ref-1 ")

        duplicate = valid_config()
        duplicate["approved_facts"].append(copy.deepcopy(duplicate["approved_facts"][0]))
        self.assert_code(duplicate, "INVALID_FACTS")

        for facts in ([], None, "not a list"):
            with self.subTest(facts=facts):
                config = valid_config()
                config["approved_facts"] = facts
                self.assert_code(config, "INVALID_FACTS")

    def test_fact_nested_schema_and_text_errors_have_distinct_codes(self):
        malformed = valid_config()
        malformed["approved_facts"][0]["extra"] = "x"
        self.assert_code(malformed, "INVALID_SCHEMA")

        missing = valid_config()
        del missing["approved_facts"][0]["source_ref"]
        self.assert_code(missing, "INVALID_SCHEMA")

        for field, value in (("text", " \t "), ("source_ref", None)):
            config = valid_config()
            config["approved_facts"][0][field] = value
            self.assert_code(config, "INVALID_FACTS")

    def test_invalid_fact_identifier_uses_identity_error_group(self):
        config = valid_config()
        config["approved_facts"][0]["fact_id"] = "bad fact id"
        self.assert_code(config, "INVALID_IDENTITY")

    def test_validation_groups_follow_the_specified_order(self):
        config = valid_config()
        config["client_id"] = "bad id"
        config["unexpected"] = True
        self.assert_code(config, "INVALID_SCHEMA")

        cases = (
            ("identity_before_offer", "INVALID_IDENTITY"),
            ("offer_before_email", "INVALID_OFFER"),
            ("email_before_approval", "INVALID_EMAIL"),
            ("approval_before_validity", "INVALID_APPROVAL_REF"),
            ("validity_before_outbound", "INVALID_VALIDITY"),
            ("outbound_before_language", "OUTBOUND_MODE_FORBIDDEN"),
            ("language_before_limits", "INVALID_LANGUAGE_CONFIG"),
            ("limits_before_facts", "INVALID_LIMITS"),
        )
        for case, expected in cases:
            with self.subTest(case=case):
                config = valid_config()
                if case == "identity_before_offer":
                    config["client_id"] = "bad id"
                    config["offer"]["name"] = " "
                elif case == "offer_before_email":
                    config["offer"]["name"] = " "
                    config["mailbox"]["sender_email"] = "invalid"
                elif case == "email_before_approval":
                    config["mailbox"]["sender_email"] = "invalid"
                    config["owner_approval_ref"] = " "
                elif case == "approval_before_validity":
                    config["owner_approval_ref"] = " "
                    config["valid_from"] = "invalid"
                elif case == "validity_before_outbound":
                    config["valid_from"] = "invalid"
                    config["release_mode"] = "LIVE"
                elif case == "outbound_before_language":
                    config["release_mode"] = "LIVE"
                    config["allowed_locales"] = []
                elif case == "language_before_limits":
                    config["allowed_locales"] = []
                    config["daily_send_limit"] = True
                elif case == "limits_before_facts":
                    config["daily_send_limit"] = True
                    config["approved_facts"] = []
                self.assert_code(config, expected)

    def test_input_and_nested_output_are_independent(self):
        config = valid_config()
        original = copy.deepcopy(config)
        result = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(config, original)

        result["offer"]["name"] = "changed result"
        result["allowed_locales"].append("de-DE")
        result["approved_facts"][0]["text"] = "changed result"
        result["approved_fact_ids"].append("extra")
        self.assertEqual(config, original)

        config["offer"]["description"] = "changed input"
        config["allowed_locales"].append("fr-FR")
        config["approved_facts"][0]["text"] = "changed input"
        self.assertEqual(result["offer"]["description"], "Synthetic configuration example.")
        self.assertEqual(result["allowed_locales"], ["en-GB", "ru-RU", "de-DE"])
        self.assertEqual(result["approved_facts"][0]["text"], "changed result")

    def test_same_input_and_now_produce_equal_results(self):
        config = valid_config()
        first = sales_first_binding.validate_first_binding(config, now=NOW)
        second = sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(first, second)

    def test_errors_never_echo_secret_sentinel(self):
        secret = "SECRET SENTINEL DO NOT ECHO"
        cases = (
            ("client_id", secret, "INVALID_IDENTITY"),
            ("release_mode", secret, "OUTBOUND_MODE_FORBIDDEN"),
        )
        for field, value, code in cases:
            config = valid_config()
            config[field] = value
            with self.assertRaises(sales_first_binding.FirstBindingError) as caught:
                sales_first_binding.validate_first_binding(config, now=NOW)
            self.assertEqual(str(caught.exception), code)
            self.assertNotIn(secret, str(caught.exception))

        config = valid_config()
        config["mailbox"]["sender_email"] = secret
        with self.assertRaises(sales_first_binding.FirstBindingError) as caught:
            sales_first_binding.validate_first_binding(config, now=NOW)
        self.assertEqual(str(caught.exception), "INVALID_EMAIL")
        self.assertNotIn(secret, str(caught.exception))

    def test_import_uses_no_application_or_network_dependencies(self):
        imported = []
        original_import = builtins.__import__

        def recording_import(name, *args, **kwargs):
            imported.append(name)
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=recording_import):
            importlib.reload(sales_first_binding)

        forbidden = ("psycopg", "socket", "requests", "http", "sales_store", "connectors")
        self.assertFalse(any(name.split(".")[0] in forbidden for name in imported))


if __name__ == "__main__":
    unittest.main()
