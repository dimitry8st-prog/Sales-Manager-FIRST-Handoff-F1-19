"""Подготовка передачи клиента менеджеру без внешних действий."""

from sales_first_handoff import assemble_handoff_context


class HandoffIntentError(ValueError):
    """Ошибка подготовки передачи с публичным кодом."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

# Точные наборы обязательных полей двух новых входов.
_RECIPIENT_FIELDS = frozenset((
    "schema_version",
    "client_id",
    "runtime_campaign_id",
    "recipient_ref",
    "approval_ref",
))

_INTEREST_FIELDS = frozenset((
    "schema_version",
    "client_id",
    "runtime_campaign_id",
    "lead_id",
    "decision",
    "evidence_ref",
))


def _check_fields(value, expected_fields):
    """Проверить тип словаря и точный набор его полей."""
    if type(value) is not dict:
        raise HandoffIntentError("INVALID_INPUT")

    if value.keys() != expected_fields:
        raise HandoffIntentError("INVALID_INPUT")


def _check_id(value):
    """Проверить ID без исправления или усечения."""
    if type(value) is not str:
        raise HandoffIntentError("INVALID_INPUT")

    if not 1 <= len(value) <= 128:
        raise HandoffIntentError("INVALID_INPUT")

    if value != value.strip():
        raise HandoffIntentError("INVALID_INPUT")

    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HandoffIntentError("INVALID_INPUT")

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise HandoffIntentError("INVALID_INPUT") from None

def build_handoff_intent(
    context: dict,
    recipient: dict,
    interest: dict,
) -> dict:
    """Проверить входы и подготовить решение о передаче."""

    # 1. Проверяем тип контекста и структуры новых входов.
    if type(context) is not dict:
        raise HandoffIntentError("INVALID_INPUT")

    _check_fields(recipient, _RECIPIENT_FIELDS)
    _check_fields(interest, _INTEREST_FIELDS)

    # 2. Проверяем точные версии двух новых структур.
    if (
        type(recipient["schema_version"]) is not str
        or recipient["schema_version"] != "FIRST_HANDOFF_RECIPIENT_V1"
    ):
        raise HandoffIntentError("INVALID_INPUT")

    if (
        type(interest["schema_version"]) is not str
        or interest["schema_version"] != "FIRST_INTEREST_EVIDENCE_V1"
    ):
        raise HandoffIntentError("INVALID_INPUT")

    # 3. Проверяем идентификаторы получателя и свидетельства.
    for field in (
        "client_id",
        "runtime_campaign_id",
        "recipient_ref",
        "approval_ref",
    ):
        _check_id(recipient[field])

    for field in (
        "client_id",
        "runtime_campaign_id",
        "lead_id",
        "evidence_ref",
    ):
        _check_id(interest[field])

    decision = interest["decision"]
    if (
        type(decision) is not str
        or decision not in ("INTEREST", "QUESTION", "UNCERTAIN")
    ):
        raise HandoffIntentError("INVALID_INPUT")

    # 4. Получатель и свидетельство должны относиться
    # к одному клиенту и одной кампании.
    for field in ("client_id", "runtime_campaign_id"):
        if recipient[field] != interest[field]:
            raise HandoffIntentError("SCOPE_MISMATCH")

    # Проверяем имеющиеся строковые поля области контекста.
    # Отсутствующие или неверно типизированные поля затем
    # отклонит принятый валидатор F1-09.
    for field in ("client_id", "runtime_campaign_id", "lead_id"):
        if type(context.get(field)) is str:
            if context[field] != interest[field]:
                raise HandoffIntentError("SCOPE_MISMATCH")

    # 5. F1-09 проверяет полный контекст и собирает копию.
    # Его ошибки передаются вызывающей программе без замены.
    assembled = assemble_handoff_context(context)

    # 6. Валидные данные без подтверждённого интереса:
    # передача не подготавливается.
    if decision != "INTEREST":
        return {
            "schema_version": "FIRST_HANDOFF_INTENT_V1",
            "status": "HOLD",
            "handoff_id": None,
            "content_sha256": None,
            "recipient_ref": None,
            "context": None,
            "code": "INTEREST_NOT_CONFIRMED",
        }

    # 7. Интерес подтверждён, контекст проверен.
    return {
        "schema_version": "FIRST_HANDOFF_INTENT_V1",
        "status": "PREPARED",
        "handoff_id": assembled["handoff_id"],
        "content_sha256": assembled["content_sha256"],
        "recipient_ref": recipient["recipient_ref"],
        "context": assembled,
        "code": "OK",
    }