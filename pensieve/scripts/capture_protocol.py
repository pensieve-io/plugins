"""Read-only compatibility checks; no credentials or transcript bytes are sent."""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

VERSION = 1
HEADER = "X-Pensieve-Capture-Protocol"
MAX_BATCH_BYTES = 262144
MAX_BATCH_EVENTS = 100
MAX_EVENT_CHARS = 32000
_cache = {}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def compatible(value: object, service: str) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("protocol_version")) is int
        and value["protocol_version"] == VERSION
        and value.get("service") == service
        and isinstance(value.get("clients"), list)
        and all(client in value["clients"] for client in ("codex", "claude"))
        and all(
            type(value.get(name)) is int and value[name] >= bound
            for name, bound in (
                ("max_batch_bytes", MAX_BATCH_BYTES),
                ("max_events", MAX_BATCH_EVENTS),
                ("max_content_chars", MAX_EVENT_CHARS),
            )
        )
    )


def check(endpoint: str, service: str, timeout: float, *, fresh=False) -> str:
    """Caller validates its fixed service URL or loopback before reaching here."""
    cached = _cache.get((endpoint, service))
    if not fresh and cached and cached[0] > time.monotonic():
        return cached[1]
    status = "unavailable"
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        request = Request(
            endpoint + "/capabilities",
            headers={"Accept": "application/json", "User-Agent": "Pensieve-Plugin-Capture/1.0"},
        )
        with opener.open(request, timeout=max(0.05, timeout)) as response:
            raw = response.read(4097)
            status = (
                "compatible"
                if len(raw) <= 4096 and compatible(json.loads(raw), service)
                else "incompatible"
            )
    except HTTPError as error:
        status = "incompatible" if error.code in {404, 405, 426} else "unavailable"
    except (OSError, URLError):
        pass
    except (ValueError, TypeError):
        status = "incompatible"
    # One hook may drain eight spools. Reuse one bounded, non-secret check,
    # including failures; a new process checks again on the next hook.
    if len(_cache) >= 16:
        _cache.clear()
    _cache[(endpoint, service)] = (time.monotonic() + 30, status)
    return status
