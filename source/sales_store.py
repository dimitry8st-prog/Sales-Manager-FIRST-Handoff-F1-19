import json
import uuid
from datetime import date

import postgres_store
import sales_rules


class NotFound(Exception):
    pass


class DuplicateLead(Exception):
    pass


class StageConflict(Exception):
    pass


class ControlBlocked(Exception):
    pass


def _event(cur, lead_id, event_type, from_stage, to_stage, evidence=None):
    cur.execute(
        f"""
        INSERT INTO {postgres_store.SCHEMA}.sales_events
            (lead_id, event_type, from_stage, to_stage, evidence, created_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
        """,
        (lead_id, event_type, from_stage, to_stage, json.dumps(evidence or {})),
    )


def _lead_for_update(cur, lead_id):
    cur.execute(
        f"SELECT * FROM {postgres_store.SCHEMA}.sales_leads WHERE lead_id=%s FOR UPDATE",
        (lead_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFound("LEAD_NOT_FOUND")
    return row


def _queue_task(
    cur, scope, payload, dedupe_key, max_attempts=8, delay_seconds=0, priority=0
):
    task_id = str(uuid.uuid4())
    cur.execute(
        f"""
        INSERT INTO {postgres_store.SCHEMA}.tasks
            (task_id, scope, payload, status, attempts, max_attempts,
             dedupe_key, priority, available_at, created_at, updated_at)
        VALUES (%s, %s, %s::jsonb, 'PENDING', 0, %s, %s, %s,
                NOW() + make_interval(secs => %s), NOW(), NOW())
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
        RETURNING task_id, scope, status, attempts, max_attempts, dedupe_key, priority, available_at
        """,
        (
            task_id, scope, json.dumps(payload), max_attempts, dedupe_key,
            priority, delay_seconds,
        ),
    )
    task = cur.fetchone()
    if task is None:
        cur.execute(
            f"SELECT task_id, scope, status, attempts, max_attempts, dedupe_key, priority, available_at "
            f"FROM {postgres_store.SCHEMA}.tasks WHERE dedupe_key=%s",
            (dedupe_key,),
        )
        task = cur.fetchone()
    return task


def _queue_next_discovery(cur, lead):
    return _queue_task(
        cur,
        "sales.discover",
        {
            "campaign_id": str(lead["campaign_id"]),
            "previous_lead_id": str(lead["lead_id"]),
            "reason": "previous_send_verified",
        },
        f"sales-discover:{lead['campaign_id']}:{lead['lead_id']}",
        priority=10,
    )


def create_campaign(name, direction, config=None):
    if not isinstance(name, str) or not name.strip():
        raise sales_rules.RuleViolation("MISSING_CAMPAIGN_NAME")
    if not isinstance(direction, str) or not direction.strip():
        raise sales_rules.RuleViolation("MISSING_DIRECTION")
    if config is not None and not isinstance(config, dict):
        raise sales_rules.RuleViolation("INVALID_CAMPAIGN_CONFIG")
    effective_config = {"mode": "AUTO", **(config or {})}
    sales_rules.validate_auto_policy(effective_config)
    campaign_id = str(uuid.uuid4())
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {postgres_store.SCHEMA}.sales_campaigns
                    (campaign_id, name, direction, status, config, created_at, updated_at)
                VALUES (%s, %s, %s, 'RUNNING', %s::jsonb, NOW(), NOW())
                RETURNING *
                """,
                (campaign_id, name.strip(), direction.strip(), json.dumps(effective_config)),
            )
            campaign = cur.fetchone()
            _queue_task(
                cur,
                "sales.discover",
                {"campaign_id": campaign_id, "reason": "campaign_started"},
                f"sales-discover:{campaign_id}:initial",
                priority=10,
            )
            return campaign


def get_campaign(campaign_id):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {postgres_store.SCHEMA}.sales_campaigns WHERE campaign_id=%s",
                (campaign_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("CAMPAIGN_NOT_FOUND")
            if row["status"] == "EXHAUSTED":
                _queue_task(
                    cur,
                    "sales.notify_alert",
                    {
                        "alert_type": "CAMPAIGN_EXHAUSTED",
                        "campaign_id": str(campaign_id),
                        "campaign_name": row["name"],
                    },
                    f"sales-alert:campaign-exhausted:{campaign_id}",
                    priority=100,
                )
            return row


def set_campaign_status(campaign_id, status):
    if status not in {"RUNNING", "PAUSED", "EXHAUSTED"}:
        raise sales_rules.RuleViolation("INVALID_CAMPAIGN_STATUS")
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_campaigns "
                "SET status=%s, updated_at=NOW() WHERE campaign_id=%s RETURNING *",
                (status, campaign_id),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("CAMPAIGN_NOT_FOUND")
            return row


def get_lead(lead_id):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {postgres_store.SCHEMA}.sales_leads WHERE lead_id=%s",
                (lead_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("LEAD_NOT_FOUND")
            return row


def get_active_lead(campaign_id):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM {postgres_store.SCHEMA}.sales_leads
                WHERE campaign_id=%s
                  AND stage NOT IN ('SENT_VERIFIED', 'RESPONSE_RECEIVED', 'REJECTED')
                ORDER BY created_at
                LIMIT 1
                """,
                (campaign_id,),
            )
            return cur.fetchone()


def ingest_lead(data):
    sales_rules.validate_ingest(data)
    campaign_id = data["campaign_id"]
    lead_id = str(uuid.uuid4())
    fingerprints = sales_rules.fingerprints(data)
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"sales-campaign:{campaign_id}",),
            )
            cur.execute(
                f"SELECT status FROM {postgres_store.SCHEMA}.sales_campaigns "
                "WHERE campaign_id=%s FOR UPDATE",
                (campaign_id,),
            )
            campaign = cur.fetchone()
            if campaign is None:
                raise NotFound("CAMPAIGN_NOT_FOUND")
            if campaign["status"] != "RUNNING":
                raise ControlBlocked(f"CAMPAIGN_{campaign['status']}")
            cur.execute(
                f"""
                SELECT lead_id, company_name, stage
                FROM {postgres_store.SCHEMA}.sales_leads
                WHERE campaign_id=%s
                  AND stage NOT IN ('SENT_VERIFIED', 'RESPONSE_RECEIVED', 'REJECTED')
                ORDER BY created_at
                LIMIT 1
                """,
                (campaign_id,),
            )
            active = cur.fetchone()
            if active is not None:
                raise ControlBlocked(f"ACTIVE_LEAD_EXISTS:{active['lead_id']}")

            for fingerprint_type, fingerprint_value in fingerprints:
                cur.execute(
                    f"SELECT lead_id, fingerprint_type, fingerprint_value "
                    f"FROM {postgres_store.SCHEMA}.sales_processed_registry "
                    "WHERE fingerprint_type=%s AND fingerprint_value=%s",
                    (fingerprint_type, fingerprint_value),
                )
                duplicate = cur.fetchone()
                if duplicate is not None:
                    raise DuplicateLead(
                        f"DUPLICATE:{fingerprint_type}:{duplicate['lead_id']}"
                    )

            cur.execute(
                f"""
                INSERT INTO {postgres_store.SCHEMA}.sales_leads
                    (lead_id, campaign_id, company_name, domain, contact_name, contact_role,
                     email, phone, source_url, source_observed_at, aliases, stage,
                     metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz,
                        %s::jsonb, 'DISCOVERED', %s::jsonb, NOW(), NOW())
                RETURNING *
                """,
                (
                    lead_id,
                    campaign_id,
                    data["company_name"].strip(),
                    sales_rules.normalize_domain(data.get("domain")) or None,
                    data.get("contact_name"),
                    data.get("contact_role"),
                    sales_rules.normalize_email(data.get("email")) or None,
                    data.get("phone"),
                    data["source_url"],
                    data["source_observed_at"],
                    json.dumps(data.get("aliases") or []),
                    json.dumps(data.get("metadata") or {}),
                ),
            )
            _event(cur, lead_id, "DISCOVERED", None, "DISCOVERED", {
                "source_url": data["source_url"],
                "source_observed_at": data["source_observed_at"],
            })
            for fingerprint_type, fingerprint_value in fingerprints:
                cur.execute(
                    f"""
                    INSERT INTO {postgres_store.SCHEMA}.sales_processed_registry
                        (fingerprint_type, fingerprint_value, lead_id, status, recorded_at)
                    VALUES (%s, %s, %s, 'ACTIVE', NOW())
                    """,
                    (fingerprint_type, fingerprint_value, lead_id),
                )
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='DUPLICATE_CLEARED', updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (lead_id,),
            )
            lead = cur.fetchone()
            _event(
                cur,
                lead_id,
                "DUPLICATE_CLEARED",
                "DISCOVERED",
                "DUPLICATE_CLEARED",
                {"fingerprints": fingerprints},
            )
            _queue_task(
                cur,
                "sales.prepare",
                {"lead_id": lead_id},
                f"sales-prepare:{lead_id}",
                priority=20,
            )
            return lead


def verify_live(lead_id, evidence):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            sales_rules.assert_transition(lead["stage"], "LIVE_VERIFIED")
            sales_rules.validate_live_evidence(evidence, lead)
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='LIVE_VERIFIED', live_evidence=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(evidence), lead_id),
            )
            result = cur.fetchone()
            _event(cur, lead_id, "LIVE_VERIFIED", lead["stage"], "LIVE_VERIFIED", evidence)
            return result


def save_gold_audit(lead_id, audit):
    sales_rules.validate_gold_audit(audit)
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            sales_rules.assert_transition(lead["stage"], "GOLD_AUDITED")
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='GOLD_AUDITED', gold_audit=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(audit), lead_id),
            )
            result = cur.fetchone()
            _event(cur, lead_id, "GOLD_AUDITED", lead["stage"], "GOLD_AUDITED", audit)
            return result


def save_materials(lead_id, materials):
    sales_rules.validate_materials(materials)
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            sales_rules.assert_transition(lead["stage"], "MATERIALS_READY")
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='MATERIALS_READY', materials=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(materials), lead_id),
            )
            _ = cur.fetchone()
            _event(cur, lead_id, "MATERIALS_READY", lead["stage"], "MATERIALS_READY", {
                "subject": materials["subject"],
                "attachment_count": len(materials.get("attachments", [])),
            })
            policy = {
                "mode": "AUTO",
                "authority": "campaign_policy",
                "gates_passed": [
                    "duplicate_control",
                    "live_verification",
                    "gold_audit",
                    "materials_validation",
                ],
            }
            sales_rules.validate_auto_policy(policy)
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='SEND_QUEUED', policy_decision=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(policy), lead_id),
            )
            result = cur.fetchone()
            _event(
                cur,
                lead_id,
                "SEND_QUEUED",
                "MATERIALS_READY",
                "SEND_QUEUED",
                policy,
            )
            _queue_task(
                cur,
                "sales.send",
                {"lead_id": lead_id},
                f"sales-send:{lead_id}",
                max_attempts=5,
                priority=50,
            )
            return result


def get_control():
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {postgres_store.SCHEMA}.sales_control WHERE id=1")
            row = cur.fetchone()
            if row is None:
                raise NotFound("SALES_CONTROL_NOT_INITIALIZED")
            return row


def set_control(emergency_stop=None, daily_send_limit=None):
    if emergency_stop is not None and not isinstance(emergency_stop, bool):
        raise sales_rules.RuleViolation("INVALID_EMERGENCY_STOP")
    if daily_send_limit is not None and (
        not isinstance(daily_send_limit, int) or not 1 <= daily_send_limit <= 1000
    ):
        raise sales_rules.RuleViolation("INVALID_DAILY_SEND_LIMIT")
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {postgres_store.SCHEMA}.sales_control
                SET emergency_stop=COALESCE(%s, emergency_stop),
                    daily_send_limit=COALESCE(%s, daily_send_limit),
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (emergency_stop, daily_send_limit),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("SALES_CONTROL_NOT_INITIALIZED")
            return row


def reserve_send_slot(lead_id):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {postgres_store.SCHEMA}.sales_control WHERE id=1 FOR UPDATE"
            )
            control = cur.fetchone()
            if control is None:
                raise NotFound("SALES_CONTROL_NOT_INITIALIZED")
            if control["counter_date"] != date.today():
                cur.execute(
                    f"UPDATE {postgres_store.SCHEMA}.sales_control "
                    "SET sent_today=0, counter_date=CURRENT_DATE, updated_at=NOW() "
                    "WHERE id=1 RETURNING *"
                )
                control = cur.fetchone()
            if control["emergency_stop"]:
                raise ControlBlocked("EMERGENCY_STOP_ACTIVE")
            lead = _lead_for_update(cur, lead_id)
            if lead["stage"] in {"SENT", "SENT_VERIFIED", "RESPONSE_RECEIVED"}:
                return lead
            if lead["stage"] != "SEND_QUEUED":
                raise StageConflict(f"EXPECTED_SEND_QUEUED:GOT_{lead['stage']}")
            if lead["send_reserved"]:
                return lead
            if control["sent_today"] >= control["daily_send_limit"]:
                raise ControlBlocked("DAILY_SEND_LIMIT_REACHED")
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_control "
                "SET sent_today=sent_today+1, updated_at=NOW() WHERE id=1"
            )
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET send_reserved=TRUE, updated_at=NOW() WHERE lead_id=%s RETURNING *",
                (lead_id,),
            )
            reserved = cur.fetchone()
            _event(cur, lead_id, "SEND_SLOT_RESERVED", lead["stage"], lead["stage"], {})
            return reserved


def release_send_slot(lead_id, reason):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {postgres_store.SCHEMA}.sales_control WHERE id=1 FOR UPDATE"
            )
            _ = cur.fetchone()
            lead = _lead_for_update(cur, lead_id)
            if lead["send_reserved"]:
                cur.execute(
                    f"UPDATE {postgres_store.SCHEMA}.sales_control "
                    "SET sent_today=GREATEST(0, sent_today-1), updated_at=NOW() WHERE id=1"
                )
                cur.execute(
                    f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                    "SET send_reserved=FALSE, updated_at=NOW() WHERE lead_id=%s",
                    (lead_id,),
                )
                _event(cur, lead_id, "SEND_SLOT_RELEASED", lead["stage"], lead["stage"], {
                    "reason": str(reason)[:500]
                })


def record_send(lead_id, receipt):
    if not isinstance(receipt, dict) or not (receipt.get("accepted") or receipt.get("sent")):
        raise sales_rules.RuleViolation("SEND_NOT_ACCEPTED")
    verified = bool(receipt.get("sent_verified"))
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            if lead["stage"] in {"SENT", "SENT_VERIFIED", "RESPONSE_RECEIVED"}:
                return lead
            if lead["stage"] != "SEND_QUEUED":
                raise StageConflict(f"EXPECTED_SEND_QUEUED:GOT_{lead['stage']}")
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='SENT', send_receipt=%s::jsonb, send_reserved=FALSE, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(receipt), lead_id),
            )
            sent = cur.fetchone()
            _event(cur, lead_id, "SENT", "SEND_QUEUED", "SENT", receipt)
            if verified:
                cur.execute(
                    f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                    "SET stage='SENT_VERIFIED', updated_at=NOW() WHERE lead_id=%s RETURNING *",
                    (lead_id,),
                )
                sent = cur.fetchone()
                _event(cur, lead_id, "SENT_VERIFIED", "SENT", "SENT_VERIFIED", receipt)
                _queue_next_discovery(cur, sent)
            else:
                _queue_task(
                    cur,
                    "sales.verify_sent",
                    {"lead_id": lead_id},
                    f"sales-verify-sent:{lead_id}",
                    delay_seconds=15,
                    priority=60,
                )
            return sent


def verify_sent(lead_id, evidence):
    if not isinstance(evidence, dict) or not evidence.get("verified"):
        raise sales_rules.RuleViolation("SENT_NOT_VERIFIED")
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            if lead["stage"] in {"SENT_VERIFIED", "RESPONSE_RECEIVED"}:
                return lead
            if lead["stage"] != "SENT":
                raise StageConflict(f"EXPECTED_SENT:GOT_{lead['stage']}")
            receipt = {**(lead.get("send_receipt") or {}), "verification": evidence}
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='SENT_VERIFIED', send_receipt=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(receipt), lead_id),
            )
            result = cur.fetchone()
            _event(cur, lead_id, "SENT_VERIFIED", "SENT", "SENT_VERIFIED", evidence)
            _queue_next_discovery(cur, result)
            return result


def record_reply(lead_id, reply):
    if not isinstance(reply, dict) or not reply.get("message"):
        raise sales_rules.RuleViolation("INVALID_REPLY")
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            if lead["stage"] == "RESPONSE_RECEIVED":
                return {"lead": lead, "task": None}
            if lead["stage"] == "SENT":
                cur.execute(
                    f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                    "SET stage='SENT_VERIFIED', updated_at=NOW() WHERE lead_id=%s",
                    (lead_id,),
                )
                _event(cur, lead_id, "SENT_VERIFIED", "SENT", "SENT_VERIFIED", {
                    "verified_by_human_reply": True
                })
                from_stage = "SENT_VERIFIED"
            elif lead["stage"] == "SENT_VERIFIED":
                from_stage = "SENT_VERIFIED"
            else:
                raise StageConflict(f"REPLY_NOT_ALLOWED_FROM_{lead['stage']}")
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='RESPONSE_RECEIVED', reply=%s::jsonb, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (json.dumps(reply), lead_id),
            )
            result = cur.fetchone()
            _event(cur, lead_id, "RESPONSE_RECEIVED", from_stage, "RESPONSE_RECEIVED", reply)
            dedupe_key = f"sales-notify-reply:{lead_id}:{reply.get('message_id', 'latest')}"
            task = _queue_task(
                cur,
                "sales.notify_reply",
                {"lead_id": lead_id},
                dedupe_key,
                priority=100,
            )
            _queue_next_discovery(cur, result)
            return {"lead": result, "task": task}


def reject_lead(lead_id, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise sales_rules.RuleViolation("MISSING_REJECTION_REASON")
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            lead = _lead_for_update(cur, lead_id)
            if lead["stage"] in {"SENT", "SENT_VERIFIED", "RESPONSE_RECEIVED"}:
                raise StageConflict(f"REJECTION_NOT_ALLOWED_FROM_{lead['stage']}")
            if lead["stage"] == "REJECTED":
                return lead
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_leads "
                "SET stage='REJECTED', rejection_reason=%s, send_reserved=FALSE, updated_at=NOW() "
                "WHERE lead_id=%s RETURNING *",
                (reason.strip(), lead_id),
            )
            result = cur.fetchone()
            _event(cur, lead_id, "REJECTED", lead["stage"], "REJECTED", {"reason": reason.strip()})
            cur.execute(
                f"UPDATE {postgres_store.SCHEMA}.sales_processed_registry "
                "SET status='REJECTED' WHERE lead_id=%s",
                (lead_id,),
            )
            _queue_task(
                cur,
                "sales.discover",
                {
                    "campaign_id": str(result["campaign_id"]),
                    "previous_lead_id": str(result["lead_id"]),
                    "reason": "lead_rejected",
                },
                f"sales-discover:{result['campaign_id']}:rejected:{result['lead_id']}",
                priority=10,
            )
            return result


def list_events(lead_id, limit=100):
    with postgres_store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {postgres_store.SCHEMA}.sales_events "
                "WHERE lead_id=%s ORDER BY event_id DESC LIMIT %s",
                (lead_id, max(1, min(limit, 1000))),
            )
            return cur.fetchall()
