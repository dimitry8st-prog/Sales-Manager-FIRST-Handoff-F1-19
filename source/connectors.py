import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ConnectorError(RuntimeError):
    pass


def post_json(url_env, token_env, event, payload, idempotency_key, timeout=30):
    url = os.getenv(url_env)
    if not url:
        raise ConnectorError(f"CONNECTOR_NOT_CONFIGURED:{url_env}")
    body = json.dumps({"event": event, "payload": payload}, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": "STRATIVON-Sales-Runtime/0.2",
    }
    token = os.getenv(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            if not 200 <= response.status < 300:
                raise ConnectorError(f"CONNECTOR_HTTP_{response.status}")
    except HTTPError as exc:
        raise ConnectorError(f"CONNECTOR_HTTP_{exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise ConnectorError(f"CONNECTOR_UNAVAILABLE:{exc}") from exc
    if not raw:
        return {}
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("CONNECTOR_INVALID_JSON") from exc
    if not isinstance(result, dict):
        raise ConnectorError("CONNECTOR_INVALID_RESPONSE")
    return result
