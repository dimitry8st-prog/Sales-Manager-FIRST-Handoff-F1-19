import json
import unittest
from copy import deepcopy
from pathlib import Path

from sales_first_handoff import HandoffContextError
from sales_handoff_intent import (
    HandoffIntentError,
    build_handoff_intent,
)


# Пример хранится внутри source и переносится вместе с тестами.
EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "wave3"
    / "f119"
    / "examples.json"
)


class HandoffIntentTests(unittest.TestCase):
    def setUp(self):
        # Каждый тест получает свежие данные из примера ТЗ.
        example = json.loads(
            EXAMPLE_PATH.read_text(
                encoding="utf-8"
            )
        )
        self.inputs = example["input"]
        self.expected = example["expected"]

    def test_interest_matches_exact_example(self):
        actual = build_handoff_intent(**self.inputs)
        self.assertEqual(actual, self.expected)

    def test_question_and_uncertain_hold(self):
        for decision in ("QUESTION", "UNCERTAIN"):
            with self.subTest(decision=decision):
                inputs = deepcopy(self.inputs)
                inputs["interest"]["decision"] = decision

                actual = build_handoff_intent(**inputs)

                self.assertEqual(actual, {
                    "schema_version": "FIRST_HANDOFF_INTENT_V1",
                    "status": "HOLD",
                    "handoff_id": None,
                    "content_sha256": None,
                    "recipient_ref": None,
                    "context": None,
                    "code": "INTEREST_NOT_CONFIRMED",
                })

    def test_prepared_context_is_not_dispatched(self):
        actual = build_handoff_intent(**self.inputs)
        self.assertEqual(
            actual["context"]["delivery_status"],
            "NOT_DISPATCHED",
        )
    def test_foreign_recipient_scope_rejected(self):
        for field in ("client_id", "runtime_campaign_id"):
            with self.subTest(field=field):
                inputs = deepcopy(self.inputs)
                inputs["recipient"][field] = "foreign"

                with self.assertRaises(HandoffIntentError) as caught:
                    build_handoff_intent(**inputs)

                self.assertEqual(
                    caught.exception.code, "SCOPE_MISMATCH"
                )
                self.assertEqual(
                    str(caught.exception), "SCOPE_MISMATCH"
                )

    def test_foreign_interest_scope_rejected(self):
        for field in ("client_id", "runtime_campaign_id", "lead_id"):
            with self.subTest(field=field):
                inputs = deepcopy(self.inputs)
                inputs["interest"][field] = "foreign"

                with self.assertRaises(HandoffIntentError) as caught:
                    build_handoff_intent(**inputs)

                self.assertEqual(
                    caught.exception.code, "SCOPE_MISMATCH"
                )
                self.assertEqual(
                    str(caught.exception), "SCOPE_MISMATCH"
                )
    def test_missing_history_rejected(self):
        inputs = deepcopy(self.inputs)
        del inputs["context"]["history"]

        with self.assertRaises(HandoffContextError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "INVALID_CONTEXT"
        )
        self.assertEqual(
            str(caught.exception), "INVALID_CONTEXT"
        )

    def test_history_over_limit_rejected_without_changes(self):
        inputs = deepcopy(self.inputs)
        original_message = inputs["context"]["history"][0]

        history = []
        for number in range(101):
            message = deepcopy(original_message)
            message["message_id"] = f"message-{number}"
            message["sequence"] = number
            history.append(message)

        inputs["context"]["history"] = history
        before = deepcopy(inputs)

        with self.assertRaises(HandoffContextError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "INVALID_CONTEXT"
        )
        self.assertEqual(inputs, before)

    def test_long_recipient_and_interest_ids_rejected(self):
        fields_by_input = {
            "recipient": (
                "client_id",
                "runtime_campaign_id",
                "recipient_ref",
                "approval_ref",
            ),
            "interest": (
                "client_id",
                "runtime_campaign_id",
                "lead_id",
                "evidence_ref",
            ),
        }

        for input_name, fields in fields_by_input.items():
            for field in fields:
                with self.subTest(input=input_name, field=field):
                    inputs = deepcopy(self.inputs)
                    inputs[input_name][field] = "x" * 129
                    before = deepcopy(inputs)

                    with self.assertRaises(HandoffIntentError) as caught:
                        build_handoff_intent(**inputs)

                    self.assertEqual(
                        caught.exception.code, "INVALID_INPUT"
                    )
                    self.assertEqual(inputs, before)

    def test_long_context_event_id_rejected(self):
        inputs = deepcopy(self.inputs)
        inputs["context"]["handoff_event_id"] = "x" * 129
        before = deepcopy(inputs)

        with self.assertRaises(HandoffContextError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "INVALID_CONTEXT"
        )
        self.assertEqual(inputs, before)

    def test_changed_body_changes_hash_with_same_handoff_id(self):
        original = build_handoff_intent(**self.inputs)

        changed_inputs = deepcopy(self.inputs)
        changed_inputs["context"]["history"][0]["body"] = (
            "Другой синтетический текст сообщения."
        )
        changed = build_handoff_intent(**changed_inputs)

        self.assertEqual(
            original["handoff_id"], changed["handoff_id"]
        )
        self.assertNotEqual(
            original["content_sha256"],
            changed["content_sha256"],
        )
        self.assertEqual(
            changed["context"]["history"][0]["body"],
            changed_inputs["context"]["history"][0]["body"],
        )

    def test_input_unchanged_and_result_detached(self):
        before = deepcopy(self.inputs)
        actual = build_handoff_intent(**self.inputs)

        # Сам вызов не должен менять вход.
        self.assertEqual(self.inputs, before)

        # Меняем вложенные объекты результата.
        actual["context"]["history"][0]["body"] = "Изменено"
        actual["context"]["history"][0]["fact_ids"].append(
            "result-only"
        )
        actual["context"]["language_packs"]["ru-RU"] = "changed"
        actual["context"]["history"].clear()

        # Исходные данные должны остаться прежними.
        self.assertEqual(self.inputs, before)

    def test_missing_and_extra_fields_rejected(self):
        for input_name in ("recipient", "interest"):
            for field in self.inputs[input_name]:
                with self.subTest(input=input_name, missing=field):
                    inputs = deepcopy(self.inputs)
                    del inputs[input_name][field]

                    with self.assertRaises(HandoffIntentError) as caught:
                        build_handoff_intent(**inputs)

                    self.assertEqual(
                        caught.exception.code, "INVALID_INPUT"
                    )

            with self.subTest(input=input_name, extra=True):
                inputs = deepcopy(self.inputs)
                inputs[input_name]["unexpected"] = "extra"

                with self.assertRaises(HandoffIntentError) as caught:
                    build_handoff_intent(**inputs)

                self.assertEqual(
                    caught.exception.code, "INVALID_INPUT"
                )

    def test_wrong_types_versions_and_decisions_rejected(self):
        for input_name in ("context", "recipient", "interest"):
            for invalid in (None, [], "text", 1, True):
                with self.subTest(input=input_name, value=invalid):
                    inputs = deepcopy(self.inputs)
                    inputs[input_name] = invalid

                    with self.assertRaises(HandoffIntentError) as caught:
                        build_handoff_intent(**inputs)

                    self.assertEqual(
                        caught.exception.code, "INVALID_INPUT"
                    )

        for input_name in ("recipient", "interest"):
            for field in self.inputs[input_name]:
                with self.subTest(input=input_name, field=field):
                    inputs = deepcopy(self.inputs)
                    inputs[input_name][field] = []

                    with self.assertRaises(HandoffIntentError) as caught:
                        build_handoff_intent(**inputs)

                    self.assertEqual(
                        caught.exception.code, "INVALID_INPUT"
                    )

            inputs = deepcopy(self.inputs)
            inputs[input_name]["schema_version"] = "WRONG_VERSION"

            with self.assertRaises(HandoffIntentError) as caught:
                build_handoff_intent(**inputs)

            self.assertEqual(
                caught.exception.code, "INVALID_INPUT"
            )

        for invalid in ("UNKNOWN", "interest", ""):
            with self.subTest(decision=invalid):
                inputs = deepcopy(self.inputs)
                inputs["interest"]["decision"] = invalid

                with self.assertRaises(HandoffIntentError) as caught:
                    build_handoff_intent(**inputs)

                self.assertEqual(
                    caught.exception.code, "INVALID_INPUT"
                )

    def test_error_precedence(self):
        inputs = deepcopy(self.inputs)

        # Три дефекта одновременно.
        inputs["recipient"]["recipient_ref"] = []
        inputs["interest"]["lead_id"] = "foreign"
        del inputs["context"]["history"]

        # Неверный тип имеет первый приоритет.
        with self.assertRaises(HandoffIntentError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "INVALID_INPUT"
        )

        # Исправляем тип: теперь обнаруживается чужой лид.
        inputs["recipient"]["recipient_ref"] = "owner-test"

        with self.assertRaises(HandoffIntentError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "SCOPE_MISMATCH"
        )

        # Исправляем лида: остаётся ошибка контекста F1-09.
        inputs["interest"]["lead_id"] = (
            self.inputs["interest"]["lead_id"]
        )

        with self.assertRaises(HandoffContextError) as caught:
            build_handoff_intent(**inputs)

        self.assertEqual(
            caught.exception.code, "INVALID_CONTEXT"
        )

    def test_invalid_context_not_hidden_by_hold(self):
        for decision in ("QUESTION", "UNCERTAIN"):
            with self.subTest(decision=decision):
                inputs = deepcopy(self.inputs)
                inputs["interest"]["decision"] = decision
                del inputs["context"]["history"]

                with self.assertRaises(HandoffContextError) as caught:
                    build_handoff_intent(**inputs)

                self.assertEqual(
                    caught.exception.code, "INVALID_CONTEXT"
                )
