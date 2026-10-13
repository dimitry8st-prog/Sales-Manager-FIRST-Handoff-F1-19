"""Canonical Make preparation boundary; no network, database or send operations."""
import copy
import sales_contracts
import sales_mailbox_contract
import sales_rules

ContractBlocked = sales_contracts.ContractBlocked


def require(ok, code):
    if not ok:
        raise ContractBlocked(code)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def build_payload(campaign, lead, task_payload):
    require(isinstance(lead, dict) and isinstance(task_payload, dict), "INVALID_PREPARATION_INPUT")
    runtime_id = sales_contracts._uuid(campaign.get("campaign_id"), "INVALID_RUNTIME_CAMPAIGN_ID")
    lead_id = sales_contracts._uuid(lead.get("lead_id"), "INVALID_PREPARATION_LEAD_ID")
    require(str(lead.get("campaign_id")) == runtime_id, "PREPARATION_LEAD_CAMPAIGN_MISMATCH")
    require(task_payload.get("lead_id") == lead_id, "PREPARATION_TASK_LEAD_MISMATCH")
    payload = sales_contracts.build_discovery_payload(campaign, {"campaign_id": runtime_id}, 1)
    payload["context"].pop("discovery")
    payload["context"]["runtime_lead"] = {"lead_id": lead_id, "campaign_id": runtime_id}
    allowed = ("company_name", "domain", "contact_name", "contact_role", "email", "phone",
               "source_url", "source_observed_at", "aliases", "metadata", "stage")
    scoped = {key: copy.deepcopy(lead[key]) for key in allowed if key in lead}
    scoped.update(lead_id=lead_id, client_id=payload["security_context"]["client_id"],
                  campaign_id=payload["campaign_config"]["campaign_id"])
    payload["lead"] = scoped
    payload["discovery_result"] = {"status": "FOUND", "lead": copy.deepcopy(scoped)}
    return payload


def _flags(response, key, fields):
    value = response.get(key)
    require(isinstance(value, dict), "PREPARATION_MISSING_" + key.upper())
    for field in fields:
        require(value.get(field) is True, "PREPARATION_GATE_FAILED:" + key + "." + field)
    return value


def normalize_response(payload, response, *, mailbox_verifier=None):
    """Normalize agent output; mailbox_verifier must be trusted server code.

    The manager supplies only the result obtained by its server-owned reader.
    Email SEND_READY responses remain blocked without that independent result.
    Never populate this dependency from payload/response metadata.
    """
    require(isinstance(response, dict), "INVALID_PREPARATION_RESPONSE")
    lead = payload["lead"]
    require(response.get("client_id") == lead["client_id"]
            and response.get("campaign_id") == lead["campaign_id"]
            and response.get("lead_id") == lead["lead_id"], "PREPARATION_RESPONSE_SCOPE_MISMATCH")
    status = response.get("status")
    require(status in {"SEND_READY", "NOT_READY"}, "PREPARATION_STATUS_INVALID")
    if status == "NOT_READY":
        require(nonempty(response.get("reason")), "PREPARATION_NOT_READY_REASON_MISSING")
        return {"status": status, "reason": response["reason"]}
    verification = _flags(response, "verification", ("passed", "company_verified", "source_current", "contact_verified"))
    require(nonempty(verification.get("contact_evidence")), "PREPARATION_CONTACT_EVIDENCE_MISSING")
    recipient = _flags(response, "recipient_resolution", ("passed",))
    require(recipient.get("type") in {"DECISION_MAKER", "CORPORATE_FALLBACK"}, "PREPARATION_RECIPIENT_TYPE_INVALID")
    if recipient["type"] == "DECISION_MAKER":
        require(recipient.get("role_verified") is True, "PREPARATION_RECIPIENT_ROLE_UNVERIFIED")
    else:
        require(recipient.get("decision_maker_search_exhausted") is True
                and nonempty(recipient.get("fallback_reason")), "PREPARATION_CORPORATE_FALLBACK_UNJUSTIFIED")
    gold = _flags(response, "gold_audit", ("passed",))
    _flags(response, "materials_qa", ("passed", "personalization_verified", "truthfulness_verified", "recipient_match_verified", "attachments_verified"))
    _flags(response, "language_qa", ("passed", "locale_supported", "grammar_passed", "subject_body_consistent", "names_preserved", "links_preserved", "no_mixed_language", "claims_preserved"))
    for key in ("materials_qa", "language_qa"):
        require(response[key].get("qa_status") == "PASSED", "PREPARATION_QA_STATUS_FAILED:" + key)
    language = response.get("language")
    require(isinstance(language, dict) and language.get("effective_locale") in
            {"en-GB", "es-ES", "da-DK", "uk-UA"}
            and language.get("qa_status") == "PASSED", "PREPARATION_LANGUAGE_NOT_VERIFIED")
    buyer = response.get("buyer_context")
    require(isinstance(buyer, dict), "PREPARATION_BUYER_CONTEXT_MISSING")
    email = lead.get("email")
    evidence = {"primary_source_url": verification.get("primary_source_url"),
                "observed_at": verification.get("observed_at"), "source_status": "LIVE",
                "freshness_verified": True, "contact_evidence": verification["contact_evidence"]}
    if email:
        require(sales_rules.normalize_email(buyer.get("email")) == sales_rules.normalize_email(email),
                "PREPARATION_RECIPIENT_CHANGED")
        validation = _flags(response, "email_validation", ("syntax_passed", "public_source_verified"))
        # Agent claims and public publication cannot establish a working mailbox.
        receipt = sales_mailbox_contract.verify(lead, mailbox_verifier)
        sales_rules.validate_url(validation.get("source_url"), "email_source_url")
        evidence.update(email_status="VERIFIED", email_evidence=verification["contact_evidence"],
                        mailbox_deliverability=receipt["status"],
                        deliverability_evidence=receipt["evidence_ref"],
                        mailbox_verification=receipt)
    else:
        require(sales_rules.normalize_phone(buyer.get("phone")) == sales_rules.normalize_phone(lead.get("phone"))
                and bool(sales_rules.normalize_phone(lead.get("phone"))), "PREPARATION_RECIPIENT_CHANGED")
        evidence.update(phone_status="PUBLIC_SOURCE_VERIFIED")
    materials = copy.deepcopy(response.get("materials"))
    sales_rules.validate_url(recipient.get("source_url"), "recipient_source_url")
    sales_rules.validate_live_evidence(evidence, lead)
    sales_rules.validate_gold_audit(gold)
    sales_rules.validate_materials(materials)
    materials.update(language=copy.deepcopy(language), language_qa=copy.deepcopy(response["language_qa"]),
                     materials_qa=copy.deepcopy(response["materials_qa"]), buyer_context=copy.deepcopy(buyer),
                     recipient_resolution=copy.deepcopy(recipient), email_validation=copy.deepcopy(response.get("email_validation", {})))
    if email:
        materials["email_validation"].update(
            mailbox_deliverability=receipt["status"],
            deliverability_evidence=receipt["evidence_ref"],
            mailbox_verification=copy.deepcopy(receipt))
    return {"status": "SEND_READY", "live_evidence": evidence, "gold_audit": copy.deepcopy(gold), "materials": materials}
