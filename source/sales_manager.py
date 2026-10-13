import connectors
import sales_contracts
import sales_preparation_contract
import sales_mailbox_reader
import sales_rules
import sales_store


def _lead_summary(lead):
    return {
        "lead_id": str(lead["lead_id"]) if lead.get("lead_id") else None,
        "campaign_id": str(lead["campaign_id"]) if lead.get("campaign_id") else None,
        "company_name": lead.get("company_name"),
        "stage": lead.get("stage"),
    }


def _assert_sales_enabled():
    if sales_store.get_control()["emergency_stop"]:
        raise sales_store.ControlBlocked("EMERGENCY_STOP_ACTIVE")


def _lead_id(task):
    payload = task.get("payload") or {}
    lead_id = payload.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id:
        raise ValueError("MISSING_LEAD_ID")
    return lead_id


def _discovery_payload(campaign, payload, candidate_number):
    try:
        return sales_contracts.build_discovery_payload(campaign, payload, candidate_number)
    except sales_contracts.ContractBlocked as exc:
        raise sales_store.ControlBlocked(str(exc)) from None


def _assert_outbound_enabled():
    try:
        sales_contracts.assert_outbound_enabled()
    except sales_contracts.ContractBlocked as exc:
        raise sales_store.ControlBlocked(str(exc)) from None


def process_discover(task):
    _assert_sales_enabled()
    payload = task.get("payload") or {}
    campaign_id = payload.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise ValueError("MISSING_CAMPAIGN_ID")
    campaign = sales_store.get_campaign(campaign_id)
    if campaign["status"] == "EXHAUSTED":
        return {"operation": "discover", "status": "EXHAUSTED", "replayed": True}
    if campaign["status"] != "RUNNING":
        raise sales_store.ControlBlocked(f"CAMPAIGN_{campaign['status']}")
    active = sales_store.get_active_lead(campaign_id)
    if active is not None:
        return {
            "operation": "discover",
            "status": "FOUND",
            "replayed": True,
            "lead": _lead_summary(active),
        }
    stable_key = task.get("dedupe_key") or f"sales-discover:{campaign_id}"
    base_key = f"{stable_key}:run:{task.get('attempts', 1)}"
    duplicate_count = 0
    for candidate_number in range(1, 21):
        request_payload = _discovery_payload(campaign, payload, candidate_number)
        response = connectors.post_json(
            "SALES_DISCOVERY_WEBHOOK_URL",
            "SALES_DISCOVERY_WEBHOOK_TOKEN",
            "sales.discover",
            request_payload,
            f"{base_key}:candidate:{candidate_number}",
        )
        try:
            sales_contracts.assert_discovery_response_scope(request_payload, response)
        except sales_contracts.ContractBlocked as exc:
            raise sales_store.ControlBlocked(str(exc)) from None
        if response.get("status") == "EXHAUSTED":
            failure_type = response.get("failure_type")
            if failure_type == "TECHNICAL_FAILURE":
                raise connectors.ConnectorError("DISCOVERY_TECHNICAL_FAILURE")
            if failure_type != "MARKET_EXHAUSTED":
                raise connectors.ConnectorError("DISCOVERY_EXHAUSTION_NOT_CONFIRMED")
            sales_store.set_campaign_status(campaign_id, "EXHAUSTED")
            return {
                "operation": "discover",
                "status": "EXHAUSTED",
                "duplicates_skipped": duplicate_count,
            }
        lead_data = response.get("lead")
        if response.get("status") != "FOUND" or not isinstance(lead_data, dict):
            raise connectors.ConnectorError("DISCOVERY_RESULT_NOT_CONFIRMED")
        lead_data = {**lead_data, "campaign_id": campaign_id}
        try:
            lead = sales_store.ingest_lead(lead_data)
            return {
                "operation": "discover",
                "status": "FOUND",
                "duplicates_skipped": duplicate_count,
                "lead": _lead_summary(lead),
            }
        except sales_store.DuplicateLead:
            duplicate_count += 1
    raise connectors.ConnectorError("DISCOVERY_DUPLICATE_LIMIT_REACHED")


def process_prepare(task):
    _assert_sales_enabled()
    lead_id = _lead_id(task)
    lead = sales_store.get_lead(lead_id)
    if lead["stage"] in {
        "SEND_QUEUED", "SENT", "SENT_VERIFIED", "RESPONSE_RECEIVED"
    }:
        return {"operation": "prepare", "replayed": True, "lead": _lead_summary(lead)}
    try:
        request_payload = sales_preparation_contract.build_payload(
            sales_store.get_campaign(str(lead["campaign_id"])), lead, task.get("payload") or {}
        )
    except sales_contracts.ContractBlocked as exc:
        raise sales_store.ControlBlocked(str(exc)) from None
    receipt = None
    if request_payload['lead'].get('email'):
        try:
            receipt = sales_mailbox_reader.read(request_payload)
        except sales_contracts.ContractBlocked as exc:
            raise connectors.ConnectorError(str(exc)) from None
        request_payload['context']['trusted_mailbox_verification'] = receipt
    response = connectors.post_json(
        "SALES_PREPARE_WEBHOOK_URL",
        "SALES_PREPARE_WEBHOOK_TOKEN",
        "sales.prepare",
        request_payload,
        f"sales-prepare:{lead_id}",
    )
    try:
        prepared = sales_preparation_contract.normalize_response(
            request_payload, response, mailbox_verifier=(lambda _: receipt) if receipt else None)
    except (sales_contracts.ContractBlocked, sales_rules.RuleViolation) as exc:
        raise connectors.ConnectorError(str(exc)) from None
    if prepared["status"] == "NOT_READY":
        raise connectors.ConnectorError("PREPARATION_NOT_READY:" + prepared["reason"])
    live_evidence, gold_audit, materials = (prepared[key] for key in ("live_evidence", "gold_audit", "materials"))
    current = sales_store.get_lead(lead_id)
    if current["stage"] == "DUPLICATE_CLEARED":
        current = sales_store.verify_live(lead_id, live_evidence)
    if current["stage"] == "LIVE_VERIFIED":
        current = sales_store.save_gold_audit(lead_id, gold_audit)
    if current["stage"] == "GOLD_AUDITED":
        current = sales_store.save_materials(lead_id, materials)
    return {"operation": "prepare", "replayed": False, "lead": _lead_summary(current)}


def process_send(task):
    _assert_outbound_enabled()
    lead_id = _lead_id(task)
    existing = sales_store.get_lead(lead_id)
    if existing["stage"] in {"SENT", "SENT_VERIFIED", "RESPONSE_RECEIVED"}:
        return {"operation": "send", "replayed": True, "lead": _lead_summary(existing)}
    lead = sales_store.reserve_send_slot(lead_id)
    try:
        response = connectors.post_json(
            "SALES_SEND_WEBHOOK_URL",
            "SALES_SEND_WEBHOOK_TOKEN",
            "sales.send",
            {
                "lead_id": lead_id,
                "company_name": lead["company_name"],
                "contact_name": lead.get("contact_name"),
                "contact_role": lead.get("contact_role"),
                "email": lead.get("email"),
                "phone": lead.get("phone"),
                "materials": lead.get("materials") or {},
            },
            f"sales-send:{lead_id}",
        )
        result = sales_store.record_send(lead_id, response)
        return {"operation": "send", "replayed": False, "lead": _lead_summary(result)}
    except Exception as exc:
        sales_store.release_send_slot(lead_id, exc)
        raise


def process_verify_sent(task):
    lead_id = _lead_id(task)
    lead = sales_store.get_lead(lead_id)
    if lead["stage"] in {"SENT_VERIFIED", "RESPONSE_RECEIVED"}:
        return {
            "operation": "verify_sent",
            "replayed": True,
            "lead": _lead_summary(lead),
        }
    response = connectors.post_json(
        "SALES_VERIFY_SENT_WEBHOOK_URL",
        "SALES_VERIFY_SENT_WEBHOOK_TOKEN",
        "sales.verify_sent",
        {"lead_id": lead_id, "send_receipt": lead.get("send_receipt") or {}},
        f"sales-verify-sent:{lead_id}",
    )
    result = sales_store.verify_sent(lead_id, response)
    return {
        "operation": "verify_sent",
        "replayed": False,
        "lead": _lead_summary(result),
    }


def process_notify_reply(task):
    _assert_outbound_enabled()
    lead_id = _lead_id(task)
    lead = sales_store.get_lead(lead_id)
    response = connectors.post_json(
        "SALES_REPLY_NOTIFY_WEBHOOK_URL",
        "SALES_REPLY_NOTIFY_WEBHOOK_TOKEN",
        "sales.reply_received",
        {
            "lead_id": lead_id,
            "company_name": lead["company_name"],
            "contact_name": lead.get("contact_name"),
            "email": lead.get("email"),
            "reply": lead.get("reply") or {},
        },
        task.get("dedupe_key") or f"sales-notify-reply:{lead_id}",
    )
    if not response.get("notified"):
        raise connectors.ConnectorError("REPLY_NOTIFICATION_NOT_CONFIRMED")
    return {"operation": "notify_reply", "notified": True, "lead_id": lead_id}


def process_notify_alert(task):
    _assert_outbound_enabled()
    payload = task.get("payload") or {}
    response = connectors.post_json(
        "SALES_ALERT_WEBHOOK_URL",
        "SALES_ALERT_WEBHOOK_TOKEN",
        "sales.operational_alert",
        payload,
        task.get("dedupe_key") or f"sales-alert:{task.get('task_id')}",
    )
    if not response.get("notified"):
        raise connectors.ConnectorError("OPERATIONAL_ALERT_NOT_CONFIRMED")
    return {"operation": "notify_alert", "notified": True}


def process_task(task):
    scope = task.get("scope")
    if scope == "sales.discover":
        return process_discover(task)
    if scope == "sales.prepare":
        return process_prepare(task)
    if scope == "sales.send":
        return process_send(task)
    if scope == "sales.verify_sent":
        return process_verify_sent(task)
    if scope == "sales.notify_reply":
        return process_notify_reply(task)
    if scope == "sales.notify_alert":
        return process_notify_alert(task)
    raise ValueError(f"UNSUPPORTED_SALES_TASK:{scope}")
