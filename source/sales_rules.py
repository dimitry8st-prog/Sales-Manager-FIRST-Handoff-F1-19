import re
from datetime import datetime
from urllib.parse import urlparse


class RuleViolation(ValueError):
    pass


STAGES = (
    "DISCOVERED",
    "DUPLICATE_CLEARED",
    "LIVE_VERIFIED",
    "GOLD_AUDITED",
    "MATERIALS_READY",
    "SEND_QUEUED",
    "SENT",
    "SENT_VERIFIED",
    "RESPONSE_RECEIVED",
)
TERMINAL_STAGES = {"RESPONSE_RECEIVED", "REJECTED"}
NEXT_STAGE = {
    "DISCOVERED": "DUPLICATE_CLEARED",
    "DUPLICATE_CLEARED": "LIVE_VERIFIED",
    "LIVE_VERIFIED": "GOLD_AUDITED",
    "GOLD_AUDITED": "MATERIALS_READY",
    "MATERIALS_READY": "SEND_QUEUED",
    "SEND_QUEUED": "SENT",
    "SENT": "SENT_VERIFIED",
    "SENT_VERIFIED": "RESPONSE_RECEIVED",
}


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise RuleViolation(f"MISSING_{name.upper()}")
    return value.strip()


def normalize_text(value):
    value = (value or "").strip().casefold()
    value = re.sub(r"[^a-z0-9а-яіїєґ]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def normalize_email(value):
    return (value or "").strip().casefold()


def normalize_domain(value):
    value = (value or "").strip().casefold()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    return digits


def validate_email(value):
    email = normalize_email(value)
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuleViolation("INVALID_EMAIL")
    return email


def validate_url(value, name):
    text = _required_text(value, name)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuleViolation(f"INVALID_{name.upper()}")
    return text


def validate_timestamp(value, name):
    text = _required_text(value, name)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuleViolation(f"INVALID_{name.upper()}") from exc
    return text


def validate_ingest(data):
    if not isinstance(data, dict):
        raise RuleViolation("INVALID_LEAD")
    _required_text(data.get("campaign_id"), "campaign_id")
    _required_text(data.get("company_name"), "company_name")
    validate_url(data.get("source_url"), "source_url")
    validate_timestamp(data.get("source_observed_at"), "source_observed_at")
    email = validate_email(data.get("email"))
    phone = normalize_phone(data.get("phone"))
    if not email and not phone:
        raise RuleViolation("MISSING_VERIFIABLE_CONTACT_ROUTE")
    aliases = data.get("aliases", [])
    if aliases is not None and (
        not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases)
    ):
        raise RuleViolation("INVALID_ALIASES")


def fingerprints(data):
    result = []
    company = normalize_text(data.get("company_name"))
    domain = normalize_domain(data.get("domain"))
    email = normalize_email(data.get("email"))
    phone = normalize_phone(data.get("phone"))
    if company:
        result.append(("company", company))
    if domain:
        result.append(("domain", domain))
    if email:
        result.append(("email", email))
    if phone:
        result.append(("phone", phone))
    for alias in data.get("aliases") or []:
        normalized = normalize_text(alias)
        if normalized:
            result.append(("alias", normalized))
    return list(dict.fromkeys(result))


def assert_transition(current, target):
    if current == "REJECTED":
        raise RuleViolation("LEAD_REJECTED")
    if NEXT_STAGE.get(current) != target:
        raise RuleViolation(f"INVALID_STAGE_TRANSITION:{current}->{target}")


def validate_live_evidence(evidence, lead):
    if not isinstance(evidence, dict):
        raise RuleViolation("INVALID_LIVE_EVIDENCE")
    validate_url(evidence.get("primary_source_url"), "primary_source_url")
    validate_timestamp(evidence.get("observed_at"), "observed_at")
    if evidence.get("source_status") != "LIVE":
        raise RuleViolation("SOURCE_NOT_LIVE")
    if evidence.get("freshness_verified") is not True:
        raise RuleViolation("FRESHNESS_NOT_VERIFIED")
    if lead.get("email"):
        if evidence.get("email_status") != "VERIFIED":
            raise RuleViolation("EMAIL_NOT_VERIFIED")
        _required_text(evidence.get("email_evidence"), "email_evidence")


def validate_gold_audit(audit):
    if not isinstance(audit, dict):
        raise RuleViolation("INVALID_GOLD_AUDIT")
    for field in ("market_signal", "deep_audit", "needs_mapping", "focused_thesis"):
        _required_text(audit.get(field), field)


def validate_materials(materials):
    if not isinstance(materials, dict):
        raise RuleViolation("INVALID_MATERIALS")
    _required_text(materials.get("subject"), "subject")
    _required_text(materials.get("body"), "body")
    attachments = materials.get("attachments", [])
    if not isinstance(attachments, list):
        raise RuleViolation("INVALID_ATTACHMENTS")
    for item in attachments:
        if not isinstance(item, dict) or not item.get("name") or not item.get("uri"):
            raise RuleViolation("INVALID_ATTACHMENT")


def validate_auto_policy(policy):
    if not isinstance(policy, dict) or policy.get("mode") != "AUTO":
        raise RuleViolation("AUTONOMOUS_CAMPAIGN_POLICY_REQUIRED")
