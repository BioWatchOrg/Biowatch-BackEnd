"""Shared helper for turning an error `httpx.Response` into a loggable string.

Only reads fields the *server* sent back (`error_description`, response body) —
never the request we issued, so it can never echo back a `client_secret` or a
bearer token even when called from an exception handler.
"""

import httpx


def safe_error_description(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and "error_description" in payload:
        return str(payload["error_description"])
    return response.text[:200]
