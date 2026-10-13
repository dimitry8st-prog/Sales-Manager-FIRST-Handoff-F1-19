"""Pure validation for a server-owned FIRST campaign binding.

This module deliberately performs no I/O and grants no permission to send.
"""

import copy
import re
from datetime import datetime, timezone
from uuid import UUID


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "client_id",
        "runtime_campaign_id",
        "make_campaign_id",
        "offer",
        "mailbox",
        "owner_approval_ref",
        "binding_version",
        "valid_from",
        "expires_at",
        "release_mode",
        "default_locale",
        "allowed_locales",
        "language_pack_versions",
        "daily_send_limit",
        "max_followups",
        "approved_facts",
    }
)
_OFFER_FIELDS = frozenset({"offer_id", "name", "description"})
_MAILBOX_FIELDS = frozenset({"mailbox_ref", "sender_email"})
_FACT_FIELDS = frozenset({"fact_id", "text", "source_ref"})

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_LOCALE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class FirstBindingError(ValueError):
    """A validation failure with a stable, non-sensitive error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise FirstBindingError(code)


def _is_identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER_RE.fullmatch(value) is not None


def _is_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _is_locale(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 64
        and _LOCALE_RE.fullmatch(value) is not None
    )


def _has_exact_keys(value: object, fields: frozenset[str]) -> bool:
    return type(value) is dict and value.keys() == fields


def _datetime_as_utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime):
        _fail(code)
    try:
        tzinfo = value.tzinfo
        offset = value.utcoffset()
    except (AttributeError, OverflowError, TypeError, ValueError, OSError):
        _fail(code)
    if tzinfo is None or offset is None:
        _fail(code)
    try:
        return value.astimezone(timezone.utc)
    except (AttributeError, OverflowError, TypeError, ValueError, OSError):
        _fail(code)


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        _fail("INVALID_VALIDITY")
    timestamp = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (AttributeError, OverflowError, TypeError, ValueError, OSError):
        _fail("INVALID_VALIDITY")
    return _datetime_as_utc(parsed, "INVALID_VALIDITY")


def validate_first_binding(config: dict, *, now: datetime) -> dict:
    """Validate and normalize a FIRST binding without changing ``config``.

    Error groups are evaluated in task order. ``INVALID_INPUT`` checks the
    function arguments first; nested object shapes are checked at their
    corresponding offer, mailbox, and facts groups.
    """

    if type(config) is not dict:
        _fail("INVALID_INPUT")
    now_utc = _datetime_as_utc(now, "INVALID_INPUT")

    # INVALID_SCHEMA: the outer object has an exact, versioned schema.
    if config.keys() != _TOP_LEVEL_FIELDS:
        _fail("INVALID_SCHEMA")
    if (
        type(config["schema_version"]) is not str
        or config["schema_version"] != "FIRST_BINDING_INPUT_V1"
    ):
        _fail("INVALID_SCHEMA")

    # INVALID_IDENTITY: identifiers are ASCII and UUIDs are canonical lowercase.
    for field in ("client_id", "make_campaign_id", "binding_version"):
        if not _is_identifier(config[field]):
            _fail("INVALID_IDENTITY")

    runtime_campaign_id = config["runtime_campaign_id"]
    if type(runtime_campaign_id) is not str:
        _fail("INVALID_IDENTITY")
    try:
        canonical_runtime_id = str(UUID(runtime_campaign_id))
    except (AttributeError, TypeError, ValueError, OverflowError):
        _fail("INVALID_IDENTITY")
    if canonical_runtime_id != runtime_campaign_id:
        _fail("INVALID_IDENTITY")

    offer = config["offer"]
    if _has_exact_keys(offer, _OFFER_FIELDS) and not _is_identifier(offer["offer_id"]):
        _fail("INVALID_IDENTITY")
    mailbox = config["mailbox"]
    if _has_exact_keys(mailbox, _MAILBOX_FIELDS) and not _is_identifier(
        mailbox["mailbox_ref"]
    ):
        _fail("INVALID_IDENTITY")
    facts = config["approved_facts"]
    if type(facts) is list:
        for fact in facts:
            if type(fact) is dict and "fact_id" in fact:
                if not _is_identifier(fact["fact_id"]):
                    _fail("INVALID_IDENTITY")

    # INVALID_OFFER: a malformed nested offer schema is reported here.
    if not _has_exact_keys(offer, _OFFER_FIELDS):
        _fail("INVALID_SCHEMA")
    if not _is_text(offer["name"]) or not _is_text(offer["description"]):
        _fail("INVALID_OFFER")

    # INVALID_EMAIL: a malformed nested mailbox schema is reported here.
    if not _has_exact_keys(mailbox, _MAILBOX_FIELDS):
        _fail("INVALID_SCHEMA")
    sender_email = mailbox["sender_email"]
    if type(sender_email) is not str:
        _fail("INVALID_EMAIL")
    normalized_email = sender_email.strip().lower()
    if not normalized_email or _EMAIL_RE.fullmatch(normalized_email) is None:
        _fail("INVALID_EMAIL")

    if not _is_text(config["owner_approval_ref"]):
        _fail("INVALID_APPROVAL_REF")

    valid_from = _parse_timestamp(config["valid_from"])
    expires_at = _parse_timestamp(config["expires_at"])
    if not valid_from < expires_at or not valid_from <= now_utc < expires_at:
        _fail("INVALID_VALIDITY")

    if type(config["release_mode"]) is not str or config["release_mode"] != "NO_OUTBOUND":
        _fail("OUTBOUND_MODE_FORBIDDEN")

    allowed_locales = config["allowed_locales"]
    if type(allowed_locales) is not list or not allowed_locales:
        _fail("INVALID_LANGUAGE_CONFIG")
    if any(not _is_locale(locale) for locale in allowed_locales):
        _fail("INVALID_LANGUAGE_CONFIG")
    if len(set(allowed_locales)) != len(allowed_locales):
        _fail("INVALID_LANGUAGE_CONFIG")
    default_locale = config["default_locale"]
    if not _is_locale(default_locale) or default_locale not in allowed_locales:
        _fail("INVALID_LANGUAGE_CONFIG")
    language_pack_versions = config["language_pack_versions"]
    if (
        type(language_pack_versions) is not dict
        or language_pack_versions.keys() != set(allowed_locales)
        or any(not _is_identifier(version) for version in language_pack_versions.values())
    ):
        _fail("INVALID_LANGUAGE_CONFIG")

    daily_send_limit = config["daily_send_limit"]
    max_followups = config["max_followups"]
    if type(daily_send_limit) is not int or daily_send_limit < 1:
        _fail("INVALID_LIMITS")
    if type(max_followups) is not int or max_followups < 0:
        _fail("INVALID_LIMITS")

    if type(facts) is not list or not facts:
        _fail("INVALID_FACTS")
    for fact in facts:
        if not _has_exact_keys(fact, _FACT_FIELDS):
            _fail("INVALID_SCHEMA")
    seen_fact_ids: set[str] = set()
    for fact in facts:
        fact_id = fact["fact_id"]
        if fact_id in seen_fact_ids:
            _fail("INVALID_FACTS")
        seen_fact_ids.add(fact_id)
        if not _is_text(fact["text"]) or not _is_text(fact["source_ref"]):
            _fail("INVALID_FACTS")

    result = copy.deepcopy(config)
    result["schema_version"] = "FIRST_BINDING_V1"
    result["mailbox"]["sender_email"] = normalized_email
    result["valid_from"] = valid_from.isoformat()
    result["expires_at"] = expires_at.isoformat()
    result["approved_fact_ids"] = [fact["fact_id"] for fact in facts]
    return result
