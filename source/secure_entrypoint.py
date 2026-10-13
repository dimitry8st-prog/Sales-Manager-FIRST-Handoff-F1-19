"""Authenticated HTTP entrypoint for the existing runtime API."""

import json
import os
import secrets
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from main import RuntimeHandler


class SecureRuntimeHandler(RuntimeHandler):
    server_version = "STRATIVONSecureRuntime/2.0"

    def _gateway_authorized(self):
        expected_key = os.getenv("MAKE_API_KEY", "")
        provided_key = self.headers.get("X-API-Key", "")
        if not expected_key or not provided_key:
            return False

        # Compare bytes so a non-ASCII header value is rejected cleanly rather
        # than making compare_digest raise TypeError for str inputs.
        try:
            return secrets.compare_digest(
                provided_key.encode("utf-8"), expected_key.encode("utf-8")
            )
        except (TypeError, UnicodeError):
            return False

    def _gateway_reject(self):
        return self._send_json(401, {
            "error": "UNAUTHORIZED",
            "gateway": "STRATIVON",
        })

    def log_message(self, fmt, *args):
        """Log access without request paths/headers that may contain secrets."""
        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "secure_gateway_http_access",
        }), flush=True)

    def do_GET(self):
        if not self._gateway_authorized():
            return self._gateway_reject()
        return super().do_GET()

    def do_POST(self):
        if not self._gateway_authorized():
            return self._gateway_reject()
        return super().do_POST()


def run_server():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), SecureRuntimeHandler)
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "secure_gateway_started",
        "build": os.getenv("SECURE_GATEWAY_BUILD_ID", "unknown"),
        "host": host,
        "port": port,
    }))
    server.serve_forever()


if __name__ == "__main__":
    run_server()
