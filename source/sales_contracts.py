"""Server-owned campaign bindings for the discovery connector.

This candidate retains the CP1 NO_OUTBOUND hold. Binding configuration is
read from the worker environment, never from a task or campaign JSON config.
"""
import copy
import json
import os
import re
from uuid import UUID

BINDINGS_ENV = "SALES_CAMPAIGN_BINDINGS_JSON"
RELEASE_MODE = "NO_OUTBOUND"


class ContractBlocked(RuntimeError):
    pass


def _require(ok, code):
    if not ok:
        raise ContractBlocked(code)


def _identifier(value):
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value))


def _uuid(value, code):
    try:
        text = str(value)
        _require(str(UUID(text)) == text, code)
        return text
    except (ValueError, TypeError, AttributeError):
        raise ContractBlocked(code) from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_BINDING_KEY")
        result[key] = value
    return result


def _load_bindings():
    raw = os.getenv(BINDINGS_ENV)
    _require(isinstance(raw, str) and bool(raw), "CAMPAIGN_BINDING_NOT_CONFIGURED")
    _require(len(raw.encode("utf-8")) <= 65536, "BINDINGS_TOO_LARGE")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError):
        raise ContractBlocked("INVALID_BINDINGS_JSON") from None
    _require(isinstance(value, dict) and type(value.get("version")) is int
             and value["version"] == 1, "INVALID_BINDINGS_VERSION")
    _require(isinstance(value.get("campaigns"), dict), "INVALID_BINDINGS_MAP")
    return value["campaigns"]


def assert_outbound_enabled():
    # Deliberately no request, campaign, or environment flag can release this hold.
    raise ContractBlocked("NO_OUTBOUND_HOLD")


def assert_discovery_response_scope(payload, response):
    _require(isinstance(response, dict), "INVALID_DISCOVERY_RESPONSE")
    if response.get("status") != "FOUND":
        return
    lead = response.get("lead")
    _require(isinstance(lead, dict), "INVALID_DISCOVERY_LEAD")
    _require(lead.get("client_id") == payload["security_context"]["client_id"]
             and lead.get("campaign_id") == payload["campaign_config"]["campaign_id"],
             "DISCOVERY_RESPONSE_SCOPE_MISMATCH")


def build_discovery_payload(campaign, task_payload, candidate_number):
    _require(isinstance(campaign, dict) and isinstance(task_payload, dict),
             "INVALID_DISCOVERY_INPUT")
    runtime_id = _uuid(campaign.get("campaign_id"), "INVALID_RUNTIME_CAMPAIGN_ID")
    _require(task_payload.get("campaign_id") == runtime_id,
             "TASK_CAMPAIGN_MISMATCH")
    _require(type(candidate_number) is int and 1 <= candidate_number <= 20,
             "INVALID_CANDIDATE_NUMBER")
    binding = _load_bindings().get(runtime_id)
    _require(isinstance(binding, dict), "CAMPAIGN_BINDING_NOT_CONFIGURED")
    client_id, make_id = binding.get("client_id"), binding.get("make_campaign_id")
    _require(_identifier(client_id) and _identifier(make_id), "INVALID_BOUND_IDENTITY")
    config = copy.deepcopy(binding.get("campaign_config"))
    _require(isinstance(config, dict), "BOUND_CAMPAIGN_CONFIG_REQUIRED")
    _require(config.get("campaign_id", make_id) == make_id,
             "BOUND_CAMPAIGN_ID_MISMATCH")
    _require(config.get("client_id", client_id) == client_id,
             "BOUND_CLIENT_ID_MISMATCH")
    config["campaign_id"] = make_id
    for field in ("company_brand", "offer_product", "business_goal"):
        _require(isinstance(config.get(field), str) and bool(config[field].strip()),
                 "BOUND_CAMPAIGN_DESCRIPTION_REQUIRED")
    resources = copy.deepcopy(binding.get("resources", {}))
    _require(isinstance(resources, dict), "INVALID_BOUND_RESOURCES")
    for kind, resource in resources.items():
        _require(kind in {"sender", "crm", "drive"} and isinstance(resource, dict),
                 "INVALID_BOUND_RESOURCE")
        _require(resource.get("owner_client_id") == client_id
                 and _identifier(resource.get("ref")), "BOUND_RESOURCE_OWNER_MISMATCH")
        permissions = resource.get("permissions")
        _require(isinstance(permissions, list)
                 and all(_identifier(p) for p in permissions),
                 "INVALID_BOUND_RESOURCE_PERMISSIONS")
    for field in ("name", "direction"):
        _require(isinstance(campaign.get(field), str) and bool(campaign[field].strip()),
                 "RUNTIME_CAMPAIGN_DESCRIPTION_REQUIRED")
    previous = task_payload.get("previous_lead_id")
    if previous is not None:
        previous = _uuid(previous, "INVALID_PREVIOUS_LEAD_ID")
    reason = task_payload.get("reason")
    _require(reason is None or isinstance(reason, str) and len(reason) <= 512,
             "INVALID_DISCOVERY_REASON")
    return {
        "dry_run": True,
        "campaign_config": config,
        "security_context": {"client_id": client_id, "resources": resources},
        "context": {
            "runtime_campaign": {
                "campaign_id": runtime_id,
                "name": campaign["name"],
                "direction": campaign["direction"],
            },
            "discovery": {
                "previous_lead_id": previous, "reason": reason,
                "candidate_number": candidate_number, "skip_duplicates": True,
            },
        },
    }
