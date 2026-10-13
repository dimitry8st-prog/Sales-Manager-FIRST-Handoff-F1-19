"""Сборка контекста передачи FIRST без внешних действий."""

import hashlib
import json
from copy import deepcopy


class HandoffContextError(ValueError):
    """Ошибка контекста, содержащая только публичный код."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_SCOPE_FIELDS = (
    "client_id",
    "runtime_campaign_id",
    "lead_id",
    "mailbox_ref",
    "thread_id",
)

_CONTEXT_ID_FIELDS = _SCOPE_FIELDS + (
    "handoff_event_id",
    "product_id",
    "policy_version",
    "locale",
    "language_pack_version",
)

_CONTEXT_FIELDS = frozenset(
    _CONTEXT_ID_FIELDS
    + (
        "schema_version",
        "status",
        "reason",
        "language_packs",
        "summary",
        "next_step",
        "history",
        "facts",
        "materials",
    )
)

_MESSAGE_FIELDS = frozenset(
    _SCOPE_FIELDS
    + (
        "message_id",
        "sequence",
        "direction",
        "actor",
        "body",
        "fact_ids",
        "material_ids",
    )
)

_FACT_FIELDS = frozenset(("fact_id", "text", "source_ref"))
_MATERIAL_FIELDS = frozenset(
    ("material_id", "name", "evidence_ref")
)


def _fail(code):
    raise HandoffContextError(code)


def _check_fields(value, fields):
    if type(value) is not dict:
        _fail("INVALID_CONTEXT")
    if value.keys() != fields:
        _fail("INVALID_CONTEXT")


def _check_id(value):
    if type(value) is not str:
        _fail("INVALID_CONTEXT")
    if not 1 <= len(value) <= 128:
        _fail("INVALID_CONTEXT")
    if value != value.strip():
        _fail("INVALID_CONTEXT")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("INVALID_CONTEXT")


def _check_text(value, maximum=20000):
    if type(value) is not str:
        _fail("INVALID_CONTEXT")
    if not 1 <= len(value) <= maximum or not value.strip():
        _fail("INVALID_CONTEXT")
    if any(
        (ord(char) < 32 and char not in "\n\t")
        or ord(char) == 127
        for char in value
    ):
        _fail("INVALID_CONTEXT")


def _check_choice(value, choices):
    if type(value) is not str or value not in choices:
        _fail("INVALID_CONTEXT")


def _check_list(value, minimum, maximum):
    if type(value) is not list:
        _fail("INVALID_CONTEXT")
    if not minimum <= len(value) <= maximum:
        _fail("INVALID_CONTEXT")


def _check_id_list(value):
    _check_list(value, 0, 50)
    for identifier in value:
        _check_id(identifier)


def _validate_structure(context):
    """Первый этап: проверка структуры всего входа."""

    _check_fields(context, _CONTEXT_FIELDS)
    _check_choice(
        context["schema_version"],
        ("FIRST_HANDOFF_INPUT_V1",),
    )

    for field in _CONTEXT_ID_FIELDS:
        _check_id(context[field])

    _check_choice(
        context["status"],
        ("ACTIVE", "HANDED_OFF", "STOPPED"),
    )
    _check_choice(
        context["reason"],
        ("INTEREST", "OUT_OF_SCOPE"),
    )

    packs = context["language_packs"]
    if type(packs) is not dict or not packs:
        _fail("INVALID_CONTEXT")

    for locale, version in packs.items():
        _check_id(locale)
        _check_id(version)

    _check_text(context["summary"], 2000)
    _check_text(context["next_step"], 2000)

    _check_list(context["history"], 1, 100)
    _check_list(context["facts"], 0, 50)
    _check_list(context["materials"], 0, 50)

    for message in context["history"]:
        _check_fields(message, _MESSAGE_FIELDS)
        _check_id(message["message_id"])

        for field in _SCOPE_FIELDS:
            _check_id(message[field])

        sequence = message["sequence"]
        if type(sequence) is not int:
            _fail("INVALID_CONTEXT")
        if not 0 <= sequence <= 1000000000:
            _fail("INVALID_CONTEXT")

        _check_choice(
            message["direction"],
            ("INBOUND", "OUTBOUND"),
        )
        _check_choice(
            message["actor"],
            ("BUYER", "AUTOMATION", "HUMAN"),
        )

        if message["direction"] == "INBOUND":
            if message["actor"] != "BUYER":
                _fail("INVALID_CONTEXT")
        elif message["actor"] not in ("AUTOMATION", "HUMAN"):
            _fail("INVALID_CONTEXT")

        _check_text(message["body"])
        _check_id_list(message["fact_ids"])
        _check_id_list(message["material_ids"])

    for fact in context["facts"]:
        _check_fields(fact, _FACT_FIELDS)
        _check_id(fact["fact_id"])
        _check_text(fact["text"], 2000)
        _check_id(fact["source_ref"])

    for material in context["materials"]:
        _check_fields(material, _MATERIAL_FIELDS)
        _check_id(material["material_id"])
        _check_id(material["name"])
        _check_id(material["evidence_ref"])


def _check_unique(values):
    if len(values) != len(set(values)):
        _fail("DUPLICATE_ID")


def assemble_handoff_context(context: dict) -> dict:
    """Проверить вход и собрать независимый пакет передачи."""

    # 1. Структура, типы, диапазоны и допустимые значения.
    _validate_structure(context)

    # 2. Соответствие выбранного языка реестру.
    locale = context["locale"]
    packs = context["language_packs"]
    if (
        locale not in packs
        or packs[locale] != context["language_pack_version"]
    ):
        _fail("LANGUAGE_SCOPE_MISMATCH")

    # 3. Принадлежность каждого сообщения области данных.
    for message in context["history"]:
        for field in _SCOPE_FIELDS:
            if message[field] != context[field]:
                _fail("MESSAGE_SCOPE_MISMATCH")

    # 4. Уникальность идентификаторов, порядка и ссылок.
    _check_unique(
        [message["message_id"] for message in context["history"]]
    )
    _check_unique(
        [message["sequence"] for message in context["history"]]
    )
    _check_unique([fact["fact_id"] for fact in context["facts"]])
    _check_unique(
        [material["material_id"] for material in context["materials"]]
    )

    for message in context["history"]:
        _check_unique(message["fact_ids"])
        _check_unique(message["material_ids"])

    # 5. Наличие объектов, на которые ссылаются сообщения.
    fact_ids = {fact["fact_id"] for fact in context["facts"]}
    material_ids = {
        material["material_id"] for material in context["materials"]
    }

    for message in context["history"]:
        if any(
            identifier not in fact_ids
            for identifier in message["fact_ids"]
        ):
            _fail("REFERENCE_NOT_FOUND")
        if any(
            identifier not in material_ids
            for identifier in message["material_ids"]
        ):
            _fail("REFERENCE_NOT_FOUND")

    # Независимая копия: входные данные не изменяются.
    payload = deepcopy(context)
    payload["schema_version"] = "FIRST_HANDOFF_CONTEXT_V1"

    payload["history"].sort(key=lambda message: message["sequence"])
    payload["facts"].sort(key=lambda fact: fact["fact_id"])
    payload["materials"].sort(
        key=lambda material: material["material_id"]
    )

    for message in payload["history"]:
        message["fact_ids"].sort()
        message["material_ids"].sort()

    # Хеш вычисляется до добавления трёх служебных полей.
    canonical_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    payload["handoff_id"] = context["handoff_event_id"]
    payload["content_sha256"] = hashlib.sha256(
        canonical_bytes
    ).hexdigest()
    payload["delivery_status"] = "NOT_DISPATCHED"

    return payload