import ast
import copy
import json
import unittest
from collections import OrderedDict

import sales_first_send_receipt as sfr
from sales_first_send_receipt import SendReceiptError, normalize_send_receipt

HASH_A = "a" * 64
HASH_B = "0123456789abcdef" * 4

RESULT_KEYS = {
    "schema_version", "client_id", "runtime_campaign_id", "lead_id", "mailbox_ref",
    "action_id", "attempt_no", "payload_sha256", "phase", "state", "reason",
    "provider_message_id", "evidence_ref", "reconciliation_required", "resend_authorized",
}
ID_FIELDS = ("client_id", "runtime_campaign_id", "lead_id", "mailbox_ref", "action_id")
SCOPE_FIELDS = ID_FIELDS + ("attempt_no", "payload_sha256")

BAD_IDS = (
    "", "x" * 129, " lead", "lead ", "\tlead", "lead\n", "le\x00ad", "le\x1fad", "le\x7fad",
    None, 1, True, b"lead", ["lead"], {"id": "lead"},
)
BAD_HASHES = (
    "a" * 63, "a" * 65, "", "A" * 64, "a" * 63 + "A", "g" * 64, "a" * 63 + " ",
    " " + "a" * 63, None, 1, b"a" * 64,
)
BAD_ATTEMPT_NOS = (True, False, 0, -1, 1_000_001, 1.0, "1", None)


def make_attempt(phase="DISPATCH_STARTED", **overrides):
    record = {
        "schema_version": "FIRST_SEND_ATTEMPT_V1",
        "client_id": "c-test",
        "runtime_campaign_id": "campaign-test",
        "lead_id": "lead-test",
        "mailbox_ref": "mb-test",
        "action_id": "action-test",
        "attempt_no": 1,
        "payload_sha256": HASH_A,
        "phase": phase,
    }
    record.update(overrides)
    return record


def make_receipt(outcome="ACCEPTED", **overrides):
    record = {
        "schema_version": "FIRST_PROVIDER_SEND_RESULT_V1",
        "client_id": "c-test",
        "runtime_campaign_id": "campaign-test",
        "lead_id": "lead-test",
        "mailbox_ref": "mb-test",
        "action_id": "action-test",
        "attempt_no": 1,
        "payload_sha256": HASH_A,
        "outcome": outcome,
        "provider_message_id": "provider-msg-test",
        "evidence_ref": "evidence-test",
        "not_sent_reason": None,
    }
    if outcome == "NOT_SENT":
        record["provider_message_id"] = None
        record["not_sent_reason"] = "PROVIDER_REJECTED_BEFORE_ACCEPTANCE"
    elif outcome in ("TIMEOUT", "UNKNOWN"):
        record["provider_message_id"] = None
        record["evidence_ref"] = None
    record.update(overrides)
    return record


class ErrorAssertions(unittest.TestCase):
    def assertCode(self, code, attempt, receipt):
        with self.assertRaises(SendReceiptError) as ctx:
            normalize_send_receipt(attempt, receipt)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(str(ctx.exception), code)
        return ctx.exception


class PositiveNormalizationTest(ErrorAssertions):
    def assertResult(self, result, attempt, state, reason, reconciliation, message_id, evidence):
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertEqual(result["schema_version"], "FIRST_SEND_RECEIPT_V1")
        for name in SCOPE_FIELDS + ("phase",):
            self.assertEqual(result[name], attempt[name])
        self.assertEqual(result["state"], state)
        self.assertEqual(result["reason"], reason)
        self.assertIs(result["reconciliation_required"], reconciliation)
        self.assertEqual(result["provider_message_id"], message_id)
        self.assertEqual(result["evidence_ref"], evidence)
        self.assertIs(result["resend_authorized"], False)

    def test_accepted(self):
        attempt, receipt = make_attempt(), make_receipt("ACCEPTED")
        self.assertResult(normalize_send_receipt(attempt, receipt), attempt, "ACCEPTED",
                          "PROVIDER_ACCEPTED", True, "provider-msg-test", "evidence-test")

    def test_delivered(self):
        attempt, receipt = make_attempt(), make_receipt("DELIVERED")
        self.assertResult(normalize_send_receipt(attempt, receipt), attempt, "DELIVERED",
                          "PROVIDER_DELIVERED", False, "provider-msg-test", "evidence-test")

    def test_not_sent_not_dispatched_prepared(self):
        attempt = make_attempt("PREPARED")
        receipt = make_receipt("NOT_SENT", not_sent_reason="NOT_DISPATCHED")
        self.assertResult(normalize_send_receipt(attempt, receipt), attempt, "NOT_SENT",
                          "KNOWN_NOT_SENT", False, None, "evidence-test")

    def test_not_sent_rejected_before_acceptance_dispatch_started(self):
        attempt = make_attempt("DISPATCH_STARTED")
        receipt = make_receipt("NOT_SENT", not_sent_reason="PROVIDER_REJECTED_BEFORE_ACCEPTANCE")
        self.assertResult(normalize_send_receipt(attempt, receipt), attempt, "NOT_SENT",
                          "KNOWN_NOT_SENT", False, None, "evidence-test")

    def test_timeout_for_both_phases(self):
        for phase in ("PREPARED", "DISPATCH_STARTED"):
            with self.subTest(phase=phase):
                attempt = make_attempt(phase)
                self.assertResult(normalize_send_receipt(attempt, make_receipt("TIMEOUT")), attempt,
                                  "UNKNOWN", "AMBIGUOUS_TIMEOUT", True, None, None)

    def test_unknown_for_both_phases(self):
        for phase in ("PREPARED", "DISPATCH_STARTED"):
            with self.subTest(phase=phase):
                attempt = make_attempt(phase)
                self.assertResult(normalize_send_receipt(attempt, make_receipt("UNKNOWN")), attempt,
                                  "UNKNOWN", "AMBIGUOUS_RESULT", True, None, None)

    def test_no_receipt_prepared(self):
        attempt = make_attempt("PREPARED")
        self.assertResult(normalize_send_receipt(attempt, None), attempt,
                          "UNKNOWN", "NO_PROVIDER_EVIDENCE", True, None, None)

    def test_no_receipt_dispatch_started(self):
        attempt = make_attempt("DISPATCH_STARTED")
        self.assertResult(normalize_send_receipt(attempt, None), attempt,
                          "UNKNOWN", "NO_PROVIDER_EVIDENCE", True, None, None)

    def test_boundary_values_are_accepted_and_preserved(self):
        values = {
            "client_id": "C" * 128,
            "runtime_campaign_id": "Campaign With Inner Spaces",
            "lead_id": "x",
            "mailbox_ref": "MB-Mixed-Case",
            "action_id": "действие-1",
            "attempt_no": 1_000_000,
            "payload_sha256": HASH_B,
        }
        attempt = make_attempt(**values)
        receipt = make_receipt("DELIVERED", **values)
        result = normalize_send_receipt(attempt, receipt)
        for name, value in values.items():
            self.assertEqual(result[name], value)
        self.assertEqual(result["state"], "DELIVERED")

    def test_parsed_json_input_is_accepted(self):
        attempt = json.loads(json.dumps(make_attempt()))
        receipt = json.loads(json.dumps(make_receipt("ACCEPTED")))
        self.assertEqual(normalize_send_receipt(attempt, receipt)["state"], "ACCEPTED")


class AttemptValidationTest(ErrorAssertions):
    def test_attempt_must_be_exact_builtin_dict(self):
        class DictSubclass(dict):
            pass

        for value in (None, [], "attempt", 1, OrderedDict(make_attempt()),
                      DictSubclass(make_attempt()), list(make_attempt().items())):
            with self.subTest(value_type=type(value).__name__):
                self.assertCode("INVALID_ATTEMPT", value, None)

    def test_missing_fields(self):
        for name in make_attempt():
            with self.subTest(missing=name):
                attempt = make_attempt()
                del attempt[name]
                self.assertCode("INVALID_ATTEMPT", attempt, None)
        self.assertCode("INVALID_ATTEMPT", {}, None)

    def test_unknown_fields_rejected_even_with_all_required(self):
        for extra in ("extra", "retry_allowed", "resend_authorized", "outcome", 1, None):
            with self.subTest(extra=extra):
                attempt = make_attempt()
                attempt[extra] = "x"
                self.assertCode("INVALID_ATTEMPT", attempt, None)

    def test_schema_version(self):
        for value in ("FIRST_SEND_ATTEMPT_V2", "first_send_attempt_v1", "FIRST_PROVIDER_SEND_RESULT_V1",
                      "FIRST_SEND_RECEIPT_V1", " FIRST_SEND_ATTEMPT_V1", "", None, 1):
            with self.subTest(value=value):
                self.assertCode("INVALID_ATTEMPT", make_attempt(schema_version=value), None)

    def test_bad_ids(self):
        for name in ID_FIELDS:
            for value in BAD_IDS:
                with self.subTest(field=name, value=value):
                    self.assertCode("INVALID_ATTEMPT", make_attempt(**{name: value}), None)

    def test_bad_attempt_no(self):
        for value in BAD_ATTEMPT_NOS:
            with self.subTest(value=value):
                self.assertCode("INVALID_ATTEMPT", make_attempt(attempt_no=value), None)

    def test_bad_hash(self):
        for value in BAD_HASHES:
            with self.subTest(value=value):
                self.assertCode("INVALID_ATTEMPT", make_attempt(payload_sha256=value), None)

    def test_bad_phase(self):
        for value in ("SENT", "prepared", "Dispatch_Started", "", " PREPARED", None, 1, ["PREPARED"]):
            with self.subTest(value=value):
                self.assertCode("INVALID_ATTEMPT", make_attempt(phase=value), None)


class ReceiptValidationTest(ErrorAssertions):
    def test_receipt_must_be_exact_builtin_dict_or_none(self):
        class DictSubclass(dict):
            pass

        for value in ([], "receipt", 0, False, OrderedDict(make_receipt()),
                      DictSubclass(make_receipt()), list(make_receipt().items())):
            with self.subTest(value_type=type(value).__name__):
                self.assertCode("INVALID_RECEIPT", make_attempt(), value)

    def test_empty_literal_receipt(self):
        self.assertCode("INVALID_RECEIPT", make_attempt(), {})

    def test_missing_fields_including_nullable(self):
        for name in make_receipt():
            with self.subTest(missing=name):
                receipt = make_receipt()
                del receipt[name]
                self.assertCode("INVALID_RECEIPT", make_attempt(), receipt)

    def test_unknown_fields_rejected_even_with_all_required(self):
        for extra in ("extra", "phase", "accepted", "http_status", "delivered_at", 7):
            with self.subTest(extra=extra):
                receipt = make_receipt()
                receipt[extra] = None
                self.assertCode("INVALID_RECEIPT", make_attempt(), receipt)

    def test_schema_version(self):
        for value in ("FIRST_PROVIDER_SEND_RESULT_V2", "FIRST_SEND_ATTEMPT_V1", "FIRST_SEND_RECEIPT_V1",
                      "first_provider_send_result_v1", "", None, 1):
            with self.subTest(value=value):
                self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(schema_version=value))

    def test_bad_ids(self):
        for name in ID_FIELDS:
            for value in BAD_IDS:
                with self.subTest(field=name, value=value):
                    self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(**{name: value}))

    def test_bad_nullable_ids(self):
        for name in ("provider_message_id", "evidence_ref"):
            for value in BAD_IDS:
                if value is None:
                    continue
                with self.subTest(field=name, value=value):
                    self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(**{name: value}))

    def test_bad_attempt_no(self):
        for value in BAD_ATTEMPT_NOS:
            with self.subTest(value=value):
                self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(attempt_no=value))

    def test_bad_hash(self):
        for value in BAD_HASHES:
            with self.subTest(value=value):
                self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(payload_sha256=value))

    def test_unknown_outcome(self):
        for value in ("SENT", "FAILED", "accepted", "BOUNCED", "", None, 200, True, ["ACCEPTED"]):
            with self.subTest(value=value):
                self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(outcome=value))

    def test_unknown_not_sent_reason(self):
        for value in ("HTTP_500", "TIMEOUT", "not_dispatched", "", False, 0, ["NOT_DISPATCHED"]):
            with self.subTest(value=value):
                receipt = make_receipt("NOT_SENT", not_sent_reason=value)
                self.assertCode("INVALID_RECEIPT", make_attempt(), receipt)

    def test_raw_http_like_payloads_are_not_receipts(self):
        for value in ({"status": 500}, {"status": 200, "body": ""}, {"accepted": False},
                      {"error": "timeout"}):
            with self.subTest(value=value):
                self.assertCode("INVALID_RECEIPT", make_attempt(), value)


class ScopeMismatchTest(ErrorAssertions):
    def test_each_scope_field_mismatch(self):
        others = {
            "client_id": "c-other",
            "runtime_campaign_id": "campaign-other",
            "lead_id": "lead-other",
            "mailbox_ref": "mb-other",
            "action_id": "different",
            "attempt_no": 2,
            "payload_sha256": HASH_B,
        }
        for name, value in others.items():
            with self.subTest(field=name):
                self.assertCode("RECEIPT_SCOPE_MISMATCH", make_attempt(), make_receipt(**{name: value}))

    def test_case_is_not_normalized(self):
        for name in ID_FIELDS:
            with self.subTest(field=name):
                receipt = make_receipt(**{name: make_attempt()[name].upper()})
                self.assertCode("RECEIPT_SCOPE_MISMATCH", make_attempt(), receipt)

    def test_mismatch_for_every_outcome_and_phase(self):
        for phase in ("PREPARED", "DISPATCH_STARTED"):
            for outcome in ("ACCEPTED", "DELIVERED", "NOT_SENT", "TIMEOUT", "UNKNOWN"):
                with self.subTest(phase=phase, outcome=outcome):
                    receipt = make_receipt(outcome, attempt_no=99)
                    self.assertCode("RECEIPT_SCOPE_MISMATCH", make_attempt(phase), receipt)


class ConsistencyTest(ErrorAssertions):
    def test_accepted_and_delivered_nullable_violations(self):
        bad = (
            {"provider_message_id": None},
            {"evidence_ref": None},
            {"provider_message_id": None, "evidence_ref": None},
            {"not_sent_reason": "NOT_DISPATCHED"},
            {"not_sent_reason": "PROVIDER_REJECTED_BEFORE_ACCEPTANCE"},
        )
        for outcome in ("ACCEPTED", "DELIVERED"):
            for overrides in bad:
                with self.subTest(outcome=outcome, overrides=overrides):
                    self.assertCode("INCONSISTENT_RECEIPT", make_attempt(),
                                    make_receipt(outcome, **overrides))

    def test_accepted_and_delivered_in_prepared(self):
        for outcome in ("ACCEPTED", "DELIVERED"):
            with self.subTest(outcome=outcome):
                self.assertCode("INCONSISTENT_RECEIPT", make_attempt("PREPARED"), make_receipt(outcome))

    def test_not_sent_nullable_violations(self):
        bad = (
            {"provider_message_id": "provider-msg-test"},
            {"evidence_ref": None},
            {"not_sent_reason": None},
        )
        for overrides in bad:
            with self.subTest(overrides=overrides):
                self.assertCode("INCONSISTENT_RECEIPT", make_attempt(),
                                make_receipt("NOT_SENT", **overrides))

    def test_not_dispatched_requires_prepared(self):
        receipt = make_receipt("NOT_SENT", not_sent_reason="NOT_DISPATCHED")
        self.assertCode("INCONSISTENT_RECEIPT", make_attempt("DISPATCH_STARTED"), receipt)

    def test_rejected_before_acceptance_requires_dispatch_started(self):
        receipt = make_receipt("NOT_SENT", not_sent_reason="PROVIDER_REJECTED_BEFORE_ACCEPTANCE")
        self.assertCode("INCONSISTENT_RECEIPT", make_attempt("PREPARED"), receipt)

    def test_timeout_and_unknown_must_carry_no_evidence(self):
        bad = (
            {"provider_message_id": "provider-msg-test"},
            {"evidence_ref": "evidence-test"},
            {"not_sent_reason": "NOT_DISPATCHED"},
            {"provider_message_id": "provider-msg-test", "evidence_ref": "evidence-test"},
        )
        for outcome in ("TIMEOUT", "UNKNOWN"):
            for phase in ("PREPARED", "DISPATCH_STARTED"):
                for overrides in bad:
                    with self.subTest(outcome=outcome, phase=phase, overrides=overrides):
                        self.assertCode("INCONSISTENT_RECEIPT", make_attempt(phase),
                                        make_receipt(outcome, **overrides))

    def test_timeout_copy_of_accepted_receipt(self):
        receipt = make_receipt("ACCEPTED")
        receipt["outcome"] = "TIMEOUT"
        self.assertCode("INCONSISTENT_RECEIPT", make_attempt(), receipt)


class ErrorPriorityTest(ErrorAssertions):
    def test_invalid_attempt_beats_everything(self):
        cases = (
            {},
            [],
            {"lead_id": "x"},
            make_receipt("TIMEOUT", provider_message_id="m", attempt_no=5),
            make_receipt(attempt_no=7),
        )
        for receipt in cases:
            with self.subTest(receipt=receipt):
                self.assertCode("INVALID_ATTEMPT", make_attempt(attempt_no=True), receipt)
        self.assertCode("INVALID_ATTEMPT", None, None)

    def test_invalid_receipt_beats_scope_and_consistency(self):
        receipt = make_receipt("TIMEOUT", lead_id="other-lead", evidence_ref="e",
                               payload_sha256="A" * 64)
        self.assertCode("INVALID_RECEIPT", make_attempt(), receipt)
        receipt = make_receipt("ACCEPTED", action_id="different", extra_field=1)
        self.assertCode("INVALID_RECEIPT", make_attempt("PREPARED"), receipt)

    def test_scope_mismatch_beats_inconsistency(self):
        receipt = make_receipt("ACCEPTED", action_id="different", provider_message_id=None)
        self.assertCode("RECEIPT_SCOPE_MISMATCH", make_attempt("PREPARED"), receipt)
        receipt = make_receipt("NOT_SENT", attempt_no=2, not_sent_reason="NOT_DISPATCHED")
        self.assertCode("RECEIPT_SCOPE_MISMATCH", make_attempt("DISPATCH_STARTED"), receipt)


class PurityAndContractTest(ErrorAssertions):
    def valid_pairs(self):
        return (
            (make_attempt(), make_receipt("ACCEPTED")),
            (make_attempt(), make_receipt("DELIVERED")),
            (make_attempt("PREPARED"), make_receipt("NOT_SENT", not_sent_reason="NOT_DISPATCHED")),
            (make_attempt(), make_receipt("NOT_SENT")),
            (make_attempt(), make_receipt("TIMEOUT")),
            (make_attempt(), make_receipt("UNKNOWN")),
            (make_attempt("PREPARED"), None),
            (make_attempt(), None),
        )

    def test_inputs_are_not_mutated_on_success(self):
        for attempt, receipt in self.valid_pairs():
            with self.subTest(receipt=receipt):
                before = (copy.deepcopy(attempt), copy.deepcopy(receipt))
                result = normalize_send_receipt(attempt, receipt)
                self.assertEqual((attempt, receipt), before)
                self.assertIsNot(result, attempt)
                self.assertIsNot(result, receipt)

    def test_inputs_are_not_mutated_on_error(self):
        cases = (
            (make_attempt(attempt_no=0), make_receipt()),
            (make_attempt(), make_receipt(extra=1)),
            (make_attempt(), make_receipt(lead_id="other")),
            (make_attempt("PREPARED"), make_receipt("ACCEPTED")),
        )
        for attempt, receipt in cases:
            before = (copy.deepcopy(attempt), copy.deepcopy(receipt))
            with self.assertRaises(SendReceiptError):
                normalize_send_receipt(attempt, receipt)
            self.assertEqual((attempt, receipt), before)

    def test_result_mutation_does_not_affect_inputs_or_later_calls(self):
        attempt, receipt = make_attempt(), make_receipt()
        first = normalize_send_receipt(attempt, receipt)
        first["resend_authorized"] = True
        first["lead_id"] = "tampered"
        second = normalize_send_receipt(attempt, receipt)
        self.assertIs(second["resend_authorized"], False)
        self.assertEqual(second["lead_id"], "lead-test")
        self.assertEqual(attempt, make_attempt())

    def test_deterministic_for_equal_json(self):
        for attempt, receipt in self.valid_pairs():
            with self.subTest(receipt=receipt):
                first = normalize_send_receipt(attempt, receipt)
                again = normalize_send_receipt(json.loads(json.dumps(attempt)),
                                               json.loads(json.dumps(receipt)))
                self.assertEqual(first, again)
                self.assertEqual(json.dumps(first), json.dumps(again))

    def test_exact_keys_and_resend_never_authorized(self):
        for attempt, receipt in self.valid_pairs():
            with self.subTest(receipt=receipt):
                result = normalize_send_receipt(attempt, receipt)
                self.assertEqual(set(result), RESULT_KEYS)
                self.assertIs(result["resend_authorized"], False)
                for forbidden in ("retry_allowed", "send_now", "delivered_at", "sales_success",
                                  "dedupe_key", "outcome", "not_sent_reason"):
                    self.assertNotIn(forbidden, result)

    def test_error_type_code_and_str(self):
        self.assertTrue(issubclass(SendReceiptError, ValueError))
        for code in ("INVALID_ATTEMPT", "INVALID_RECEIPT", "RECEIPT_SCOPE_MISMATCH",
                     "INCONSISTENT_RECEIPT"):
            error = SendReceiptError(code)
            self.assertIsInstance(error, ValueError)
            self.assertIsInstance(error.code, str)
            self.assertEqual(error.code, code)
            self.assertEqual(str(error), code)

    def test_errors_do_not_leak_input_values(self):
        secret_email = "person@example.com"
        secret_token = "tok-SECRET-123"
        cases = (
            ("INVALID_ATTEMPT", make_attempt(lead_id=secret_email + " "), None),
            ("INVALID_ATTEMPT", dict(make_attempt(), body="Dear person, " + secret_token), None),
            ("INVALID_RECEIPT", make_attempt(), dict(make_receipt(), token=secret_token)),
            ("RECEIPT_SCOPE_MISMATCH", make_attempt(), make_receipt(lead_id=secret_email)),
            ("INCONSISTENT_RECEIPT", make_attempt(),
             make_receipt("TIMEOUT", provider_message_id=secret_token)),
        )
        for code, attempt, receipt in cases:
            with self.subTest(code=code):
                error = self.assertCode(code, attempt, receipt)
                rendered = " ".join((str(error), repr(error), repr(error.args)))
                for secret in (secret_email, secret_token, "lead-test", "c-test"):
                    self.assertNotIn(secret, rendered)
                self.assertEqual(error.args, (code,))
                self.assertIsNone(error.__cause__)

    def test_no_raw_key_or_type_errors_escape(self):
        weird = (None, 0, "", [], (), set(), object(), {"schema_version": []},
                 {None: None}, {("a",): 1})
        for value in weird:
            with self.subTest(attempt=repr(value)):
                self.assertCode("INVALID_ATTEMPT", value, None)
            with self.subTest(receipt=repr(value)):
                if value is None:
                    continue
                self.assertCode("INVALID_RECEIPT", make_attempt(), value)
        for field in ("outcome", "not_sent_reason", "provider_message_id"):
            with self.subTest(unhashable_field=field):
                self.assertCode("INVALID_RECEIPT", make_attempt(), make_receipt(**{field: {"x": 1}}))
        self.assertCode("INVALID_ATTEMPT", make_attempt(phase={"x": 1}), None)

    def test_module_has_no_imports_io_or_global_state_mutation(self):
        with open(sfr.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        forbidden_calls = {"open", "print", "input", "exec", "eval", "compile", "__import__",
                           "globals", "setattr", "delattr"}
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls)


if __name__ == "__main__":
    unittest.main()
