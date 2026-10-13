import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import postgres_store
import sales_rules
import sales_store
import task_store


SERVICE_NAME = "strativon-sales-runtime"
VERSION = "0.2.0"
MAX_BODY_BYTES = 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RuntimeStore:
    def get_state(self, scope):
        stored = postgres_store.get_state(scope)
        if stored is not None:
            return stored
        return {"scope": scope, "version": 0, "value": {}, "updated_at": None}

    def execute(self, scope, expected_version, patch, idempotency_key):
        if not isinstance(scope, str) or not scope:
            raise ValueError("INVALID_SCOPE")
        if not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("INVALID_EXPECTED_VERSION")
        if not isinstance(patch, dict):
            raise ValueError("INVALID_PATCH")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        return postgres_store.execute_atomic(scope, expected_version, patch, idempotency_key)

    def get_audit(self, limit=100, trace_id=None):
        return postgres_store.get_audit(limit=limit, trace_id=trace_id)


store = RuntimeStore()


class RuntimeHandler(BaseHTTPRequestHandler):
    server_version = "STRATIVONRuntime/0.2"

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("INVALID_CONTENT_LENGTH") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("PAYLOAD_TOO_LARGE")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON_OBJECT_REQUIRED")
        return payload

    def _segments(self, path):
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def _send_exception(self, exc):
        if isinstance(exc, sales_store.NotFound):
            return self._send_json(404, {"error": str(exc)})
        if isinstance(exc, (sales_store.DuplicateLead, sales_store.StageConflict)):
            return self._send_json(409, {"error": str(exc)})
        if isinstance(exc, sales_store.ControlBlocked):
            return self._send_json(423, {"error": str(exc)})
        if isinstance(exc, postgres_store.StateVersionConflict):
            return self._send_json(409, {"error": "VERSION_CONFLICT"})
        if isinstance(exc, (sales_rules.RuleViolation, ValueError, json.JSONDecodeError)):
            return self._send_json(400, {"error": str(exc)})
        print(json.dumps({
            "timestamp": utc_now(),
            "type": "request_error",
            "error": type(exc).__name__,
            "message": str(exc)[:1000],
        }), flush=True)
        return self._send_json(500, {"error": "INTERNAL_ERROR"})

    def log_message(self, fmt, *args):
        print(json.dumps({
            "timestamp": utc_now(),
            "type": "http_access",
            "message": fmt % args,
        }), flush=True)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        segments = self._segments(path)
        try:
            if path == "/health":
                return self._send_json(200, {
                    "status": "HEALTHY",
                    "service": SERVICE_NAME,
                    "version": VERSION,
                    "timestamp": utc_now(),
                })
            if path == "/ready":
                postgres_store.ping()
                return self._send_json(200, {"status": "READY", "database": "READY"})
            if path.startswith("/state/"):
                scope = unquote(path[len("/state/"):])
                if not scope:
                    raise ValueError("INVALID_SCOPE")
                return self._send_json(200, store.get_state(scope))
            if path == "/audit":
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    limit = 100
                trace_id = query.get("trace_id", [None])[0]
                records = store.get_audit(
                    limit=max(1, min(limit, 1000)), trace_id=trace_id
                )
                return self._send_json(200, {"count": len(records), "records": records})
            if len(segments) == 2 and segments[0] == "tasks":
                task = task_store.get_task(segments[1])
                if task is None:
                    raise sales_store.NotFound("TASK_NOT_FOUND")
                return self._send_json(200, task)
            if path == "/sales/control":
                return self._send_json(200, sales_store.get_control())
            if len(segments) == 3 and segments[:2] == ["sales", "campaigns"]:
                return self._send_json(200, sales_store.get_campaign(segments[2]))
            if len(segments) == 3 and segments[:2] == ["sales", "leads"]:
                return self._send_json(200, sales_store.get_lead(segments[2]))
            if (
                len(segments) == 4
                and segments[:2] == ["sales", "leads"]
                and segments[3] == "events"
            ):
                records = sales_store.list_events(segments[2])
                return self._send_json(200, {"count": len(records), "records": records})
            return self._send_json(404, {"error": "NOT_FOUND"})
        except Exception as exc:
            return self._send_exception(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        segments = self._segments(path)
        try:
            payload = self._read_json()
            if path == "/tasks":
                task = task_store.create_task(
                    payload.get("scope"),
                    payload.get("payload", {}),
                    payload.get("max_attempts", 3),
                    payload.get("dedupe_key"),
                    payload.get("priority", 0),
                )
                return self._send_json(202, task)
            if path == "/execute":
                receipt = store.execute(
                    scope=payload.get("scope"),
                    expected_version=payload.get("expected_version"),
                    patch=payload.get("patch"),
                    idempotency_key=payload.get("idempotency_key"),
                )
                return self._send_json(200, receipt)
            if path == "/sales/campaigns":
                campaign = sales_store.create_campaign(
                    payload.get("name"), payload.get("direction"), payload.get("config")
                )
                return self._send_json(201, campaign)
            if path == "/sales/leads":
                return self._send_json(201, sales_store.ingest_lead(payload))
            if path == "/sales/control":
                return self._send_json(200, sales_store.set_control(
                    payload.get("emergency_stop"), payload.get("daily_send_limit")
                ))
            if (
                len(segments) == 4
                and segments[:2] == ["sales", "campaigns"]
                and segments[3] == "status"
            ):
                return self._send_json(
                    200, sales_store.set_campaign_status(segments[2], payload.get("status"))
                )
            if len(segments) == 4 and segments[:2] == ["sales", "leads"]:
                lead_id, action = segments[2], segments[3]
                if action == "verify":
                    return self._send_json(200, sales_store.verify_live(lead_id, payload))
                if action == "audit":
                    return self._send_json(200, sales_store.save_gold_audit(lead_id, payload))
                if action == "materials":
                    return self._send_json(200, sales_store.save_materials(lead_id, payload))
                if action == "verify-sent":
                    return self._send_json(200, sales_store.verify_sent(lead_id, payload))
                if action == "reply":
                    return self._send_json(202, sales_store.record_reply(lead_id, payload))
                if action == "reject":
                    return self._send_json(
                        200, sales_store.reject_lead(lead_id, payload.get("reason"))
                    )
            return self._send_json(404, {"error": "NOT_FOUND"})
        except Exception as exc:
            return self._send_exception(exc)


def run_server(host="0.0.0.0", port=None):
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), RuntimeHandler)
    print(json.dumps({
        "timestamp": utc_now(),
        "event": "runtime_started",
        "service": SERVICE_NAME,
        "version": VERSION,
        "host": host,
        "port": port,
    }), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_server()
