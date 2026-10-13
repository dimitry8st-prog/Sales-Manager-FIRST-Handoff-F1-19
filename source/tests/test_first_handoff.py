import hashlib
import json
import unittest
from copy import deepcopy

from sales_first_handoff import (
    HandoffContextError,
    assemble_handoff_context,
)


def make_context():
    """Полный синтетический пример из ТЗ."""
    return {
        "schema_version": "FIRST_HANDOFF_INPUT_V1",
        "client_id": "c-test",
        "runtime_campaign_id": "campaign-test",
        "lead_id": "lead-test",
        "mailbox_ref": "mb-test",
        "thread_id": "thread-test",
        "handoff_event_id": "handoff-test",
        "product_id": "product-test",
        "policy_version": "policy-test",
        "status": "HANDED_OFF",
        "reason": "INTEREST",
        "locale": "ru-RU",
        "language_pack_version": "ru-test-v1",
        "language_packs": {
            "en-GB": "en-test-v1",
            "ru-RU": "ru-test-v1",
        },
        "summary": "Синтетический клиент просит обсудить предложение.",
        "next_step": "Проверить согласованный следующий шаг.",
        "history": [
            {
                "message_id": "msg-test",
                "client_id": "c-test",
                "runtime_campaign_id": "campaign-test",
                "lead_id": "lead-test",
                "mailbox_ref": "mb-test",
                "thread_id": "thread-test",
                "sequence": 0,
                "direction": "INBOUND",
                "actor": "BUYER",
                "body": "Синтетический пример: обсудим предложение.",
                "fact_ids": [],
                "material_ids": [],
            }
        ],
        "facts": [],
        "materials": [],
    }


class FirstHandoffTest(unittest.TestCase):
    def assert_context_error(self, context, code):
        with self.assertRaises(HandoffContextError) as caught:
            assemble_handoff_context(context)

        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_minimal_result(self):
        context = make_context()
        result = assemble_handoff_context(context)

        expected_keys = set(context) | {
            "handoff_id",
            "content_sha256",
            "delivery_status",
        }
        self.assertIs(type(result), dict)
        self.assertEqual(set(result), expected_keys)
        self.assertEqual(
            result["schema_version"],
            "FIRST_HANDOFF_CONTEXT_V1",
        )
        self.assertEqual(result["handoff_id"], "handoff-test")
        self.assertEqual(result["delivery_status"], "NOT_DISPATCHED")

        for field in context:
            if field != "schema_version":
                with self.subTest(field=field):
                    self.assertEqual(result[field], context[field])

    def test_hash_matches_independent_calculation(self):
        context = make_context()

        # Ожидаемый payload создаём независимо от целевой функции.
        expected_payload = deepcopy(context)
        expected_payload["schema_version"] = (
            "FIRST_HANDOFF_CONTEXT_V1"
        )
        canonical_bytes = json.dumps(
            expected_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_hash = hashlib.sha256(canonical_bytes).hexdigest()

        result = assemble_handoff_context(context)
        self.assertEqual(result["content_sha256"], expected_hash)

    def test_russian_and_english_text_preserved(self):
        cases = (
            ("ru-RU", "ru-test-v1", "  Обсудим предложение.\n\tСпасибо!  "),
            ("en-GB", "en-test-v1", "  Let's discuss the offer.\n\tThanks!  "),
        )

        for locale, version, text in cases:
            with self.subTest(locale=locale):
                context = make_context()
                context["locale"] = locale
                context["language_pack_version"] = version
                context["summary"] = text
                context["next_step"] = text
                context["history"][0]["body"] = text

                result = assemble_handoff_context(context)

                self.assertEqual(result["summary"], text)
                self.assertEqual(result["next_step"], text)
                self.assertEqual(result["history"][0]["body"], text)
                self.assertEqual(result["locale"], locale)

    def test_empty_context_rejected(self):
        self.assert_context_error({}, "INVALID_CONTEXT")

    def test_additional_language_and_large_registry(self):
        context = make_context()
        context["language_packs"].update(
            {
                f"synthetic-{number}": f"version-{number}"
                for number in range(150)
            }
        )
        context["locale"] = "synthetic-149"
        context["language_pack_version"] = "version-149"

        result = assemble_handoff_context(context)

        self.assertEqual(result["locale"], "synthetic-149")
        self.assertEqual(
            result["language_pack_version"],
            "version-149",
        )
        self.assertEqual(
            result["language_packs"],
            context["language_packs"],
        )
        self.assertEqual(len(result["language_packs"]), 152)

    def test_unknown_locale_rejected(self):
        context = make_context()
        context["locale"] = "de-DE"

        self.assert_context_error(
            context,
            "LANGUAGE_SCOPE_MISMATCH",
        )

    def test_language_version_mismatch_rejected(self):
        context = make_context()
        context["language_pack_version"] = "wrong-version"

        self.assert_context_error(
            context,
            "LANGUAGE_SCOPE_MISMATCH",
        )

    def test_each_message_scope_field_rejected(self):
        fields = (
            "client_id",
            "runtime_campaign_id",
            "lead_id",
            "mailbox_ref",
            "thread_id",
        )

        for field in fields:
            with self.subTest(field=field):
                context = make_context()
                context["history"][0][field] = "foreign"

                self.assert_context_error(
                    context,
                    "MESSAGE_SCOPE_MISMATCH",
                )

    def test_foreign_message_at_end_rejected(self):
        context = make_context()
        second_message = deepcopy(context["history"][0])
        second_message["message_id"] = "msg-second"
        second_message["sequence"] = 1
        second_message["mailbox_ref"] = "foreign"
        context["history"].append(second_message)

        self.assert_context_error(
            context,
            "MESSAGE_SCOPE_MISMATCH",
        )
    def test_duplicate_message_id_and_sequence_rejected(self):
        for field in ("message_id", "sequence"):
            with self.subTest(field=field):
                context = make_context()
                second = deepcopy(context["history"][0])
                second["message_id"] = "msg-second"
                second["sequence"] = 1
                second[field] = context["history"][0][field]
                context["history"].append(second)

                self.assert_context_error(context, "DUPLICATE_ID")

    def test_duplicate_fact_and_material_ids_rejected(self):
        cases = (
            (
                "facts",
                {
                    "fact_id": "fact-one",
                    "text": "Подтверждённый синтетический факт.",
                    "source_ref": "evidence-one",
                },
            ),
            (
                "materials",
                {
                    "material_id": "material-one",
                    "name": "Synthetic-material",
                    "evidence_ref": "evidence-one",
                },
            ),
        )

        for field, item in cases:
            with self.subTest(field=field):
                context = make_context()
                context[field] = [deepcopy(item), deepcopy(item)]

                self.assert_context_error(context, "DUPLICATE_ID")

    def test_duplicate_references_rejected(self):
        for field in ("fact_ids", "material_ids"):
            with self.subTest(field=field):
                context = make_context()
                context["history"][0][field] = ["same", "same"]

                # Дубли проверяются раньше отсутствующих ссылок.
                self.assert_context_error(context, "DUPLICATE_ID")

    def test_missing_references_rejected(self):
        for field in ("fact_ids", "material_ids"):
            with self.subTest(field=field):
                context = make_context()
                context["history"][0][field] = ["missing"]

                self.assert_context_error(
                    context,
                    "REFERENCE_NOT_FOUND",
                )

    def test_existing_references_preserved(self):
        context = make_context()
        context["facts"] = [
            {
                "fact_id": "fact-one",
                "text": "Подтверждённый синтетический факт.",
                "source_ref": "evidence-fact",
            }
        ]
        context["materials"] = [
            {
                "material_id": "material-one",
                "name": "Synthetic-material",
                "evidence_ref": "evidence-material",
            }
        ]
        context["history"][0]["fact_ids"] = ["fact-one"]
        context["history"][0]["material_ids"] = ["material-one"]

        result = assemble_handoff_context(context)

        self.assertEqual(result["facts"], context["facts"])
        self.assertEqual(result["materials"], context["materials"])
        self.assertEqual(
            result["history"][0]["fact_ids"],
            ["fact-one"],
        )
        self.assertEqual(
            result["history"][0]["material_ids"],
            ["material-one"],
        )


    def test_all_statuses_preserve_human_and_buyer_messages(self):
        for status in ("ACTIVE", "HANDED_OFF", "STOPPED"):
            with self.subTest(status=status):
                context = make_context()
                context["status"] = status

                human = deepcopy(context["history"][0])
                human.update(
                    message_id="msg-human",
                    sequence=1,
                    direction="OUTBOUND",
                    actor="HUMAN",
                    body="Ответ менеджера.",
                )

                buyer = deepcopy(context["history"][0])
                buyer.update(
                    message_id="msg-new-buyer",
                    sequence=2,
                    body="Новое сообщение клиента после передачи.",
                )

                context["history"].extend([human, buyer])
                result = assemble_handoff_context(context)

                self.assertEqual(result["status"], status)
                self.assertEqual(result["history"], context["history"])
                self.assertEqual(len(result["history"]), 3)

    def test_invalid_direction_actor_combinations_rejected(self):
        cases = (
            ("INBOUND", "AUTOMATION"),
            ("INBOUND", "HUMAN"),
            ("OUTBOUND", "BUYER"),
            ("UNKNOWN", "BUYER"),
            ("OUTBOUND", "UNKNOWN"),
        )

        for direction, actor in cases:
            with self.subTest(direction=direction, actor=actor):
                context = make_context()
                context["history"][0]["direction"] = direction
                context["history"][0]["actor"] = actor

                self.assert_context_error(context, "INVALID_CONTEXT")

    def test_changed_body_changes_hash_but_preserves_handoff_id(self):
        original = make_context()
        changed = deepcopy(original)
        changed["history"][0]["body"] = "Изменённый текст сообщения."

        first = assemble_handoff_context(original)
        second = assemble_handoff_context(changed)

        self.assertEqual(first["handoff_id"], second["handoff_id"])
        self.assertNotEqual(
            first["content_sha256"],
            second["content_sha256"],
        )

    def test_input_unchanged_and_result_detached(self):
        context = make_context()
        context["facts"] = [
            {
                "fact_id": "fact-one",
                "text": "Синтетический факт.",
                "source_ref": "evidence-fact",
            }
        ]
        context["materials"] = [
            {
                "material_id": "material-one",
                "name": "Synthetic-material",
                "evidence_ref": "evidence-material",
            }
        ]
        context["history"][0]["fact_ids"] = ["fact-one"]
        context["history"][0]["material_ids"] = ["material-one"]
        before = deepcopy(context)

        result = assemble_handoff_context(context)
        self.assertEqual(context, before)

        result["language_packs"]["ru-RU"] = "changed"
        result["history"][0]["body"] = "Изменено в результате."
        result["history"][0]["fact_ids"].append("another-fact")
        result["history"][0]["material_ids"].clear()
        result["facts"][0]["text"] = "Изменённый факт."
        result["materials"][0]["name"] = "Changed-material"
        result["history"].append({})
        result["facts"].clear()
        result["materials"].clear()

        self.assertEqual(context, before)


    def test_missing_and_extra_fields_rejected(self):
        base = make_context()
        base["facts"] = [
            {
                "fact_id": "fact-one",
                "text": "Синтетический факт.",
                "source_ref": "evidence-one",
            }
        ]
        base["materials"] = [
            {
                "material_id": "material-one",
                "name": "Synthetic-material",
                "evidence_ref": "evidence-one",
            }
        ]

        for section in ("context", "history", "facts", "materials"):
            original = (
                base if section == "context" else base[section][0]
            )

            for field in original:
                with self.subTest(section=section, missing=field):
                    context = deepcopy(base)
                    target = (
                        context
                        if section == "context"
                        else context[section][0]
                    )
                    del target[field]
                    self.assert_context_error(
                        context,
                        "INVALID_CONTEXT",
                    )

            with self.subTest(section=section, extra=True):
                context = deepcopy(base)
                target = (
                    context
                    if section == "context"
                    else context[section][0]
                )
                target["unexpected"] = "extra"
                self.assert_context_error(context, "INVALID_CONTEXT")

    def test_wrong_types_rejected(self):
        for value in (None, [], "context", 1, True):
            with self.subTest(top_level=repr(value)):
                self.assert_context_error(value, "INVALID_CONTEXT")

        cases = (
            ("client_id", 123),
            ("summary", []),
            ("next_step", None),
            ("history", ()),
            ("facts", {}),
            ("materials", ""),
            ("language_packs", []),
            ("schema_version", 1),
            ("status", []),
            ("reason", {}),
        )

        for field, value in cases:
            with self.subTest(field=field):
                context = make_context()
                context[field] = value
                self.assert_context_error(context, "INVALID_CONTEXT")

        for value in (True, False, 1.0, "1", None, -1, 1000000001):
            with self.subTest(sequence=repr(value)):
                context = make_context()
                context["history"][0]["sequence"] = value
                self.assert_context_error(context, "INVALID_CONTEXT")

        for value in (0, 1000000000):
            with self.subTest(valid_sequence=value):
                context = make_context()
                context["history"][0]["sequence"] = value
                result = assemble_handoff_context(context)
                self.assertEqual(
                    result["history"][0]["sequence"],
                    value,
                )

    def test_builtin_subclasses_rejected(self):
        class CustomDict(dict):
            pass

        class CustomList(list):
            pass

        class CustomStr(str):
            pass

        class CustomInt(int):
            pass

        self.assert_context_error(
            CustomDict(make_context()),
            "INVALID_CONTEXT",
        )

        cases = (
            ("client_id", CustomStr("c-test")),
            ("history", CustomList(make_context()["history"])),
            ("language_packs", CustomDict({"ru-RU": "ru-test-v1"})),
        )

        for field, value in cases:
            with self.subTest(field=field):
                context = make_context()
                context[field] = value
                self.assert_context_error(context, "INVALID_CONTEXT")

        context = make_context()
        context["history"][0]["sequence"] = CustomInt(0)
        self.assert_context_error(context, "INVALID_CONTEXT")

    def test_error_priority(self):
        context = make_context()
        context["locale"] = "unknown-language"
        context["history"][0]["mailbox_ref"] = "foreign"
        context["history"][0]["fact_ids"] = ["missing", "missing"]

        second = deepcopy(context["history"][0])
        context["history"].append(second)

        # Даже дефект последнего сообщения проверяется
        # раньше языка, области данных, дублей и ссылок.
        context["history"][-1]["body"] = None
        self.assert_context_error(context, "INVALID_CONTEXT")

        context["history"][-1]["body"] = "Синтетический текст."
        self.assert_context_error(
            context,
            "LANGUAGE_SCOPE_MISMATCH",
        )

        context["locale"] = "ru-RU"
        self.assert_context_error(
            context,
            "MESSAGE_SCOPE_MISMATCH",
        )

        for message in context["history"]:
            message["mailbox_ref"] = context["mailbox_ref"]
        self.assert_context_error(context, "DUPLICATE_ID")

        context["history"][1]["message_id"] = "msg-second"
        context["history"][1]["sequence"] = 1
        for message in context["history"]:
            message["fact_ids"] = ["missing"]

        self.assert_context_error(context, "REFERENCE_NOT_FOUND")


    def test_id_boundaries_and_control_characters(self):
        for value in ("x", "x" * 128, "Mixed-Case_ID"):
            with self.subTest(valid_id=value):
                context = make_context()
                context["handoff_event_id"] = value
                result = assemble_handoff_context(context)
                self.assertEqual(result["handoff_id"], value)

        invalid_values = (
            "",
            "x" * 129,
            " leading",
            "trailing ",
            "\tidentifier",
        ) + tuple(
            "id" + chr(number) + "value"
            for number in (*range(32), 127)
        )

        for value in invalid_values:
            with self.subTest(invalid_id=repr(value)):
                context = make_context()
                context["handoff_event_id"] = value
                self.assert_context_error(context, "INVALID_CONTEXT")

    def test_text_boundaries_and_control_characters(self):
        for field in ("summary", "next_step", "body", "fact_text"):
            maximum = 20000 if field == "body" else 2000

            for length, valid in (
                (0, False),
                (1, True),
                (maximum, True),
                (maximum + 1, False),
            ):
                with self.subTest(field=field, length=length):
                    context = make_context()
                    text = "x" * length

                    if field == "body":
                        context["history"][0]["body"] = text
                    elif field == "fact_text":
                        context["facts"] = [
                            {
                                "fact_id": "fact-one",
                                "text": text,
                                "source_ref": "evidence-one",
                            }
                        ]
                    else:
                        context[field] = text

                    if valid:
                        result = assemble_handoff_context(context)
                        if field == "body":
                            actual = result["history"][0]["body"]
                        elif field == "fact_text":
                            actual = result["facts"][0]["text"]
                        else:
                            actual = result[field]
                        self.assertEqual(actual, text)
                    else:
                        self.assert_context_error(
                            context,
                            "INVALID_CONTEXT",
                        )

        invalid_texts = (" ", "\n\t") + tuple(
            "text" + chr(number)
            for number in (*range(32), 127)
            if number not in (9, 10)
        )

        for text in invalid_texts:
            with self.subTest(invalid_text=repr(text)):
                context = make_context()
                context["history"][0]["body"] = text
                self.assert_context_error(context, "INVALID_CONTEXT")

        context = make_context()
        context["history"][0]["body"] = "  Текст\n\tс табуляцией  "
        result = assemble_handoff_context(context)
        self.assertEqual(
            result["history"][0]["body"],
            "  Текст\n\tс табуляцией  ",
        )


    def test_history_size_boundaries(self):
        for size, valid in (
            (0, False),
            (1, True),
            (100, True),
            (101, False),
        ):
            with self.subTest(size=size):
                context = make_context()
                template = context["history"][0]
                context["history"] = []

                for number in range(size):
                    message = deepcopy(template)
                    message["message_id"] = f"msg-{number}"
                    message["sequence"] = number
                    context["history"].append(message)

                if valid:
                    result = assemble_handoff_context(context)
                    self.assertEqual(len(result["history"]), size)
                else:
                    self.assert_context_error(
                        context,
                        "INVALID_CONTEXT",
                    )

    def test_fact_material_and_reference_size_boundaries(self):
        for size, valid in ((0, True), (50, True), (51, False)):
            for section in ("facts", "materials"):
                with self.subTest(size=size, section=section):
                    context = make_context()

                    if section == "facts":
                        context["facts"] = [
                            {
                                "fact_id": f"fact-{number}",
                                "text": "Синтетический факт.",
                                "source_ref": f"evidence-{number}",
                            }
                            for number in range(size)
                        ]
                    else:
                        context["materials"] = [
                            {
                                "material_id": f"material-{number}",
                                "name": f"Material-{number}",
                                "evidence_ref": f"evidence-{number}",
                            }
                            for number in range(size)
                        ]

                    if valid:
                        result = assemble_handoff_context(context)
                        self.assertEqual(len(result[section]), size)
                    else:
                        self.assert_context_error(
                            context,
                            "INVALID_CONTEXT",
                        )

        for field in ("fact_ids", "material_ids"):
            context = make_context()
            context["history"][0][field] = [
                f"reference-{number}" for number in range(51)
            ]
            with self.subTest(oversized_references=field):
                self.assert_context_error(context, "INVALID_CONTEXT")

        context = make_context()
        context["facts"] = [
            {
                "fact_id": f"fact-{number}",
                "text": "Синтетический факт.",
                "source_ref": f"evidence-{number}",
            }
            for number in range(50)
        ]
        context["materials"] = [
            {
                "material_id": f"material-{number}",
                "name": f"Material-{number}",
                "evidence_ref": f"evidence-{number}",
            }
            for number in range(50)
        ]
        context["history"][0]["fact_ids"] = [
            item["fact_id"] for item in context["facts"]
        ]
        context["history"][0]["material_ids"] = [
            item["material_id"] for item in context["materials"]
        ]

        result = assemble_handoff_context(context)
        self.assertEqual(len(result["history"][0]["fact_ids"]), 50)
        self.assertEqual(len(result["history"][0]["material_ids"]), 50)

    def test_permutations_produce_same_sorted_result(self):
        context = make_context()
        context["facts"] = [
            {
                "fact_id": identifier,
                "text": f"Факт {identifier}.",
                "source_ref": f"evidence-{identifier}",
            }
            for identifier in ("z", "a", "B")
        ]
        context["materials"] = [
            {
                "material_id": identifier,
                "name": f"Material-{identifier}",
                "evidence_ref": f"evidence-{identifier}",
            }
            for identifier in ("z", "a", "B")
        ]

        template = context["history"][0]
        context["history"] = []
        for sequence in (2, 0, 1):
            message = deepcopy(template)
            message["message_id"] = f"msg-{sequence}"
            message["sequence"] = sequence
            message["fact_ids"] = ["z", "a", "B"]
            message["material_ids"] = ["z", "a", "B"]
            context["history"].append(message)

        before = deepcopy(context)
        permuted = deepcopy(context)
        permuted["history"].reverse()
        permuted["facts"].reverse()
        permuted["materials"].reverse()
        permuted["language_packs"] = dict(
            reversed(list(permuted["language_packs"].items()))
        )
        for message in permuted["history"]:
            message["fact_ids"].reverse()
            message["material_ids"].reverse()

        first = assemble_handoff_context(context)
        second = assemble_handoff_context(permuted)

        self.assertEqual(first, second)
        self.assertEqual(context, before)
        self.assertEqual(
            [message["sequence"] for message in first["history"]],
            [0, 1, 2],
        )
        self.assertEqual(
            [fact["fact_id"] for fact in first["facts"]],
            ["B", "a", "z"],
        )
        self.assertEqual(
            [item["material_id"] for item in first["materials"]],
            ["B", "a", "z"],
        )
        for message in first["history"]:
            self.assertEqual(message["fact_ids"], ["B", "a", "z"])
            self.assertEqual(message["material_ids"], ["B", "a", "z"])

        expected = deepcopy(before)
        expected["schema_version"] = "FIRST_HANDOFF_CONTEXT_V1"
        expected["history"].sort(key=lambda item: item["sequence"])
        expected["facts"].sort(key=lambda item: item["fact_id"])
        expected["materials"].sort(key=lambda item: item["material_id"])
        for message in expected["history"]:
            message["fact_ids"].sort()
            message["material_ids"].sort()

        expected_hash = hashlib.sha256(
            json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first["content_sha256"], expected_hash)


if __name__ == "__main__":
    unittest.main()