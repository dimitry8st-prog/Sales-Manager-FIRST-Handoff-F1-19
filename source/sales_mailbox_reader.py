"""Server-owned Hunter reader through the authenticated Make preparation route.

Only this transport response is authority. Request/task/agent receipts are ignored.
One request per invocation; queue policy owns retries. No customer messages.
"""
import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import connectors
import sales_contracts
import sales_mailbox_contract
import sales_rules


def require(ok, code):
    if not ok:
        raise sales_contracts.ContractBlocked(code)


def read(payload):
    lead = payload['lead']
    scope = {k: lead[k] for k in ('client_id', 'campaign_id', 'lead_id')}
    scope['email'] = sales_rules.validate_email(lead.get('email'))
    require(bool(scope['email']), 'PREPARATION_MAILBOX_EMAIL_REQUIRED')
    require(scope['client_id'] == payload['security_context']['client_id']
            and scope['campaign_id'] == payload['campaign_config']['campaign_id'],
            'PREPARATION_MAILBOX_REQUEST_SCOPE_MISMATCH')
    url = os.getenv('SALES_PREPARE_WEBHOOK_URL', '')
    token = os.getenv('SALES_PREPARE_WEBHOOK_TOKEN', '')
    endpoint = urlsplit(url)
    require(endpoint.scheme == 'https' and endpoint.hostname == 'hook.eu1.make.com'
            and endpoint.port is None and not endpoint.username and not endpoint.password
            and not endpoint.query and not endpoint.fragment and bool(endpoint.path.strip('/'))
            and bool(token), 'PREPARATION_MAILBOX_TRANSPORT_NOT_CONFIGURED')
    request_id = str(uuid4())
    request = copy.deepcopy(payload)
    request['lead']['email'] = scope['email']
    request['context']['mailbox_verification_only'] = True
    request['context']['mailbox_request_id'] = request_id
    request['context'].pop('trusted_mailbox_verification', None)
    try:
        result = connectors.post_json('SALES_PREPARE_WEBHOOK_URL', 'SALES_PREPARE_WEBHOOK_TOKEN',
            'sales.prepare', request, 'mailbox:' + request_id, timeout=120)
    except Exception:
        raise sales_contracts.ContractBlocked('PREPARATION_MAILBOX_VERIFIER_UNAVAILABLE') from None
    require(isinstance(result, dict)
            and result.get('schema_version') == 'HUNTER_MAILBOX_RESULT_V1'
            and result.get('provider') == 'hunter', 'PREPARATION_MAILBOX_PROVIDER_RESPONSE_INVALID')
    require(result.get('request_id') == request_id
            and all(result.get(k) == v for k, v in scope.items()),
            'PREPARATION_MAILBOX_RECEIPT_SCOPE_MISMATCH')
    data = result.get('data')
    require(isinstance(data, dict)
            and sales_rules.normalize_email(data.get('email')) == scope['email'],
            'PREPARATION_MAILBOX_PROVIDER_EMAIL_MISMATCH')
    require(data.get('status') == 'valid'
            and data.get('result') in (None, '', 'deliverable')
            and all(data.get(k) is True for k in ('regexp', 'mx_records', 'smtp_server', 'smtp_check'))
            and all(data.get(k) is False for k in ('accept_all', 'disposable', 'block', 'gibberish')),
            'PREPARATION_MAILBOX_DELIVERABILITY_UNVERIFIED')
    checked = sales_mailbox_contract._timestamp(result.get('checked_at'))
    now = datetime.now(timezone.utc)
    require(timedelta(0) <= now - checked <= timedelta(minutes=5),
            'PREPARATION_MAILBOX_RECEIPT_STALE_OR_FUTURE')
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    receipt = dict(scope, schema_version='MAILBOX_VERIFICATION_V1', method='PROVIDER_API',
                   status='DELIVERABLE', catch_all=False, provider='hunter',
                   verification_id=request_id, evidence_ref='hunter:sha256:' + digest,
                   checked_at=checked.isoformat(), expires_at=(checked + timedelta(hours=24)).isoformat())
    return sales_mailbox_contract.verify(lead, lambda _: copy.deepcopy(receipt), now=now)
