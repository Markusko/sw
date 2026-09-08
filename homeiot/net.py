"""Tiny HTTP/JSON client helpers on top of the standard library.

Hue bridges serve HTTPS with a self-signed certificate whose subject is the
bridge id, so certificate verification can never succeed without pinning the
bridge's own CA.  We therefore talk to *local bridge addresses only* with
verification disabled -- everything else (the cloud discovery endpoint) uses
the normal verified context.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from email.message import Message as EmailMessage
from typing import Any, Mapping

TIMEOUT = 6.0

INSECURE_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
INSECURE_CONTEXT.check_hostname = False
INSECURE_CONTEXT.verify_mode = ssl.CERT_NONE


class HttpError(Exception):
    def __init__(self, message: str, status: int = 0, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: Any = None,
    timeout: float = TIMEOUT,
    insecure: bool = False,
    raw_body: bytes | None = None,
) -> tuple[int, Any]:
    """Perform a request and decode a JSON body when there is one.

    `payload` is encoded as JSON; `raw_body` is sent exactly as given, for the
    protocols that are not JSON (SOAP, for one).
    """
    status, body, _headers = request_with_headers(
        method, url, headers=headers, payload=payload, timeout=timeout, insecure=insecure, raw_body=raw_body)
    return status, body


def request_with_headers(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: Any = None,
    timeout: float = TIMEOUT,
    insecure: bool = False,
    raw_body: bytes | None = None,
) -> tuple[int, Any, EmailMessage]:
    """Like `request`, but also hands back the response headers.

    A cookie-based session (Swisscom's Internet-Box, for one) is set with a
    `Set-Cookie` a JSON body never carries, so reading it needs the headers
    `request` otherwise throws away.

    The headers come back as the response's own container rather than a dict:
    a dict keeps one value per name, and a login that sets two cookies -- the
    Internet-Box does -- would lose one of them silently, leaving a session
    that fails exactly like a wrong password.  `get` still reads a header
    case-insensitively; `get_all` reads every value of a repeated one.
    """
    body = raw_body if raw_body is not None else (None if payload is None else json.dumps(payload).encode())
    all_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        all_headers.setdefault("Content-Type", "application/json")
    plea = urllib.request.Request(url, data=body, headers=all_headers, method=method.upper())
    context = INSECURE_CONTEXT if insecure and url.startswith("https") else None
    try:
        with urllib.request.urlopen(plea, timeout=timeout, context=context) as response:
            return response.status, _decode(response.read()), response.headers
    except urllib.error.HTTPError as error:  # the bridge explains itself in the body
        raise HttpError(f"{method} {url} -> HTTP {error.code}", error.code, _decode(error.read()))
    except (urllib.error.URLError, socket.timeout, OSError) as error:
        raise HttpError(f"{method} {url} -> {error}") from error


def get_json(url: str, **kwargs: Any) -> Any:
    return request("GET", url, **kwargs)[1]


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")


def local_ip() -> str:
    """Best guess at the address other machines on the LAN can reach us on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere, never sends
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()
