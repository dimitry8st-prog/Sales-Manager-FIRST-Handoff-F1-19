"""F1-08: pure normalizer for a FIRST send attempt result.

Turns a trusted server-side attempt record and an internal trusted adapter
envelope into one unambiguous FIRST_SEND_RECEIPT_V1 result.

The function performs no I/O, sends nothing, keeps no state and never
authorizes a resend. Standard library only.
"""

ATTEMPT_SCHEMA = "FIRST_SEND_ATTEMPT_V1"
RECEIPT_SCHEMA = "FIRST_PROVIDER_SEND_RESULT_V1"
RESULT_SCHEMA = "FIRST_SEND_RECEIPT_V1"

INVALID_ATTEMPT = "INVALID_ATTEMPT"
INVALID_RECEIPT = "INVALID_RECEIPT"
RECEIPT_SCOPE_MISMATCH = "RECEIPT_SCOPE_MISMATCH"
INCONSISTENT_RECEIPT = "INCONSISTENT_RECEIPT"

_ID_FIELDS = ("client_id", "runtime_campaign_id", "lead_id", "mailbox_ref", "action_id")
_SCOPE_FIELDS = _ID_FIELDS + ("attempt_no", "payload_sha256")

_ATTEMPT_FIELDS = frozenset(("schema_version",) + _SCOPE_FIELDS + ("phase",))
_RECEIPT_FIELDS = frozenset(
    ("schema_version",)
    + _SCOPE_FIELDS
    + ("outcome", "provider_message_id", "evidence_ref", "not_sent_reason")
)

_PHASES = frozenset(("PREPARED", "DISPATCH_STARTED"))
_OUTCOMES = frozenset(("ACCEPTED", "DELIVERED", "NOT_SENT", "TIMEOUT", "UNKNOWN"))
_NOT_SENT_PHASE = {
    "NOT_DISPATCHED": "PREPARED",
    "PROVIDER_REJECTED_BEFORE_ACCEPTANCE": "DISPATCH_STARTED",
}

_ID_MAX_LEN = 128
_ATTEMPT_NO_MIN = 1
_ATTEMPT_NO_MAX = 1_000_000
_HASH_LEN = 64
_HEX_LOWER = frozenset("0123456789abcdef")

# outcome -> (state, reason, reconciliation_required)
_NORMALIZATION = {
    "ACCEPTED": ("ACCEPTED", "PROVIDER_ACCEPTED", True),
    "DELIVERED": ("DELIVERED", "PROVIDER_DELIVERED", False),
    "NOT_SENT": ("NOT_SENT", "KNOWN_NOT_SENT", False),
    "TIMEOUT": ("UNKNOWN", "AMBIGUOUS_TIMEOUT", True),
    "UNKNOWN": ("UNKNOWN", "AMBIGUOUS_RESULT", True),
}
_NO_EVIDENCE = ("UNKNOWN", "NO_PROVIDER_EVIDENCE", True)


class SendReceiptError(ValueError):
    """Contract error. str(error) is the code only; input data is never included."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self._code = code

    @property
    def code(self) -> str:
        return self._code

    def __str__(self) -> str:
        return self._code


def _is_id(value) -> bool:
    if type(value) is not str:
        return False
    if not 1 <= len(value) <= _ID_MAX_LEN:
        return False
    if value != value.strip():
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _is_attempt_no(value) -> bool:
    return type(value) is int and _ATTEMPT_NO_MIN <= value <= _ATTEMPT_NO_MAX


def _is_sha256(value) -> bool:
    return (
        type(value) is str
        and len(value) == _HASH_LEN
        and all(ch in _HEX_LOWER for ch in value)
    )


def _is_optional_id(value) -> bool:
    return value is None or _is_id(value)


def _scope_valid(record: dict) -> bool:
    return (
        all(_is_id(record[name]) for name in _ID_FIELDS)
        and _is_attempt_no(record["attempt_no"])
        and _is_sha256(record["payload_sha256"])
    )


def _attempt_valid(attempt) -> bool:
    if type(attempt) is not dict or attempt.keys() != _ATTEMPT_FIELDS:
        return False
    return (
        type(attempt["schema_version"]) is str
        and attempt["schema_version"] == ATTEMPT_SCHEMA
        and _scope_valid(attempt)
        and type(attempt["phase"]) is str
        and attempt["phase"] in _PHASES
    )


def _receipt_valid(receipt) -> bool:
    if type(receipt) is not dict or receipt.keys() != _RECEIPT_FIELDS:
        return False
    reason = receipt["not_sent_reason"]
    return (
        type(receipt["schema_version"]) is str
        and receipt["schema_version"] == RECEIPT_SCHEMA
        and _scope_valid(receipt)
        and type(receipt["outcome"]) is str
        and receipt["outcome"] in _OUTCOMES
        and _is_optional_id(receipt["provider_message_id"])
        and _is_optional_id(receipt["evidence_ref"])
        and (reason is None or (type(reason) is str and reason in _NOT_SENT_PHASE))
    )


def _receipt_consistent(receipt: dict, phase: str) -> bool:
    outcome = receipt["outcome"]
    message_id = receipt["provider_message_id"]
    evidence = receipt["evidence_ref"]
    reason = receipt["not_sent_reason"]
    if outcome in ("ACCEPTED", "DELIVERED"):
        return (
            phase == "DISPATCH_STARTED"
            and message_id is not None
            and evidence is not None
            and reason is None
        )
    if outcome == "NOT_SENT":
        return (
            message_id is None
            and evidence is not None
            and reason is not None
            and _NOT_SENT_PHASE[reason] == phase
        )
    # TIMEOUT / UNKNOWN: no evidence of any kind may be attached.
    return message_id is None and evidence is None and reason is None


def normalize_send_receipt(attempt: dict, receipt: dict | None) -> dict:
    """Return a FIRST_SEND_RECEIPT_V1 dict or raise SendReceiptError.

    Error priority: INVALID_ATTEMPT, INVALID_RECEIPT, RECEIPT_SCOPE_MISMATCH,
    INCONSISTENT_RECEIPT. Inputs are never mutated; resend is never authorized.
    """
    if not _attempt_valid(attempt):
        raise SendReceiptError(INVALID_ATTEMPT)

    if receipt is None:
        state, reason, reconciliation_required = _NO_EVIDENCE
        message_id = None
        evidence = None
    else:
        if not _receipt_valid(receipt):
            raise SendReceiptError(INVALID_RECEIPT)
        if any(attempt[name] != receipt[name] for name in _SCOPE_FIELDS):
            raise SendReceiptError(RECEIPT_SCOPE_MISMATCH)
        if not _receipt_consistent(receipt, attempt["phase"]):
            raise SendReceiptError(INCONSISTENT_RECEIPT)
        state, reason, reconciliation_required = _NORMALIZATION[receipt["outcome"]]
        message_id = receipt["provider_message_id"]
        evidence = receipt["evidence_ref"]

    return {
        "schema_version": RESULT_SCHEMA,
        "client_id": attempt["client_id"],
        "runtime_campaign_id": attempt["runtime_campaign_id"],
        "lead_id": attempt["lead_id"],
        "mailbox_ref": attempt["mailbox_ref"],
        "action_id": attempt["action_id"],
        "attempt_no": attempt["attempt_no"],
        "payload_sha256": attempt["payload_sha256"],
        "phase": attempt["phase"],
        "state": state,
        "reason": reason,
        "provider_message_id": message_id,
        "evidence_ref": evidence,
        "reconciliation_required": reconciliation_required,
        "resend_authorized": False,
    }
