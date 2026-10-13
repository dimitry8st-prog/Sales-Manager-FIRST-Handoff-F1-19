"""Validate results obtained by a server-owned mailbox verification reader.

The reader is trusted application code, never JSON from Make, a task, a lead,
or a campaign. This module validates its result; it does not implement a
provider connection or establish deliverability by itself. No reader means
email preparation is blocked. NO_OUTBOUND is unaffected.
"""
import copy
from datetime import datetime, timedelta, timezone

import sales_contracts
import sales_rules

MAX_AGE = timedelta(hours=24)


def _require(ok, code):
    if not ok:
        raise sales_contracts.ContractBlocked(code)


def _timestamp(value):
    _require(isinstance(value, str) and bool(value.strip()),
             "PREPARATION_MAILBOX_RECEIPT_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise sales_contracts.ContractBlocked("PREPARATION_MAILBOX_RECEIPT_TIME_INVALID") from None
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
             "PREPARATION_MAILBOX_RECEIPT_TIME_INVALID")
    return parsed


def verify(lead, reader, *, now=None):
    _require(callable(reader), "PREPARATION_MAILBOX_VERIFIER_NOT_CONNECTED")
    scope = {key: lead[key] for key in ("client_id", "campaign_id", "lead_id")}
    scope["email"] = sales_rules.validate_email(lead.get("email"))
    _require(bool(scope["email"]), "PREPARATION_MAILBOX_EMAIL_REQUIRED")
    try:
        receipt = reader(copy.deepcopy(scope))
    except Exception:
        raise sales_contracts.ContractBlocked("PREPARATION_MAILBOX_VERIFIER_UNAVAILABLE") from None
    _require(isinstance(receipt, dict), "PREPARATION_MAILBOX_RECEIPT_MISSING")
    _require(receipt.get("schema_version") == "MAILBOX_VERIFICATION_V1",
             "PREPARATION_MAILBOX_RECEIPT_SCHEMA_INVALID")
    _require(all(receipt.get(key) == value for key, value in scope.items()),
             "PREPARATION_MAILBOX_RECEIPT_SCOPE_MISMATCH")
    _require(receipt.get("method") == "PROVIDER_API"
             and receipt.get("status") == "DELIVERABLE"
             and receipt.get("catch_all") is False,
             "PREPARATION_MAILBOX_DELIVERABILITY_UNVERIFIED")
    for key in ("provider", "verification_id", "evidence_ref"):
        value = receipt.get(key)
        _require(isinstance(value, str) and bool(value.strip()),
                 "PREPARATION_MAILBOX_RECEIPT_PROVENANCE_MISSING")
    checked, expires = (_timestamp(receipt.get(key)) for key in ("checked_at", "expires_at"))
    current = now if now is not None else datetime.now(timezone.utc)
    _require(isinstance(current, datetime) and current.tzinfo is not None,
             "PREPARATION_MAILBOX_RECEIPT_TIME_INVALID")
    _require(checked <= current < expires and current - checked <= MAX_AGE
             and timedelta(0) < expires - checked <= MAX_AGE,
             "PREPARATION_MAILBOX_RECEIPT_STALE_OR_FUTURE")
    # Return only contract fields. Unrelated provider fields may contain PII.
    keys = (*scope, "schema_version", "method", "status", "catch_all", "provider",
            "verification_id", "evidence_ref", "checked_at", "expires_at")
    return {key: copy.deepcopy(receipt[key]) for key in keys}
