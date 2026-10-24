"""Bounded recovery for public discovery GETs, with TLS verification intact.

urllib is the default and only required client. After a transport failure, curl
can retry through the platform's other TLS stack. Headers go through stdin, so
an optional GitHub token never appears in the process command line.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

_RETRY_HTTP = {408, 500, 502, 503, 504}
_BAD_BODY = (ValueError, EOFError, ET.ParseError, zlib.error)


def _curl(url: str, headers: dict[str, str], timeout: float, *, direct: bool = False) -> bytes:
    executable = shutil.which("curl")
    if not executable:
        raise OSError("curl is unavailable")
    config = []
    for key, value in headers.items():
        if any(c in key + value for c in "\r\n\x00"):
            raise ValueError("invalid HTTP header")
        line = (key + ": " + value).replace("\\", "\\\\").replace('"', '\\"')
        config.append(f'header = "{line}"\n')
    with tempfile.TemporaryDirectory(prefix="radar-http-") as directory:
        body = Path(directory) / "body"
        head = Path(directory) / "headers"
        args = [executable, "--disable", "--silent", "--show-error", "--location",
             "--max-redirs", "3", "--proto", "=https", "--proto-redir", "=https",
             "--connect-timeout", str(min(8, timeout)), "--max-time", str(timeout),
             "--dump-header", str(head), "--output", str(body),
             "--write-out", "%{http_code}", "--config", "-", "--url", url]
        if direct:
            # Bypass only the explicit HTTP proxy for this public request. A
            # system VPN/TUN can still carry it; do not call this a separate WAN.
            args[2:2] = ["--noproxy", "*"]
        result = subprocess.run(
            args,
            input="".join(config), capture_output=True, text=True, timeout=timeout + 1,
        )
        if result.returncode:
            # Do not include response bodies, headers, or arbitrary stderr in logs.
            if result.returncode in {51, 60, 77}:
                raise ssl.SSLCertVerificationError("curl certificate verification failed")
            error = urllib.error.URLError(f"curl transport error {result.returncode}")
            error.curl_code = result.returncode
            raise error
        code = int(result.stdout.strip())
        if code >= 400:
            blocks = head.read_bytes().split(b"\r\n\r\n")
            block = next((b for b in reversed(blocks) if b.startswith(b"HTTP/")), b"")
            response_headers = http.client.parse_headers(io.BytesIO(block.partition(b"\r\n")[2] + b"\r\n\r\n"))
            raise urllib.error.HTTPError(url, code, http.client.responses.get(code, "HTTP error"), response_headers, None)
        if not 200 <= code < 300:
            raise urllib.error.URLError(f"curl unexpected HTTP status {code}")
        return body.read_bytes()


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRY_HTTP
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception):
        return _retryable(exc.reason)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return False
    return isinstance(exc, (OSError, urllib.error.URLError, http.client.HTTPException, *_BAD_BODY))


def _retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        value = exc.headers.get("Retry-After")
        if value:
            try:
                return max(0, float(value))
            except ValueError:
                try:
                    return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
    return float(attempt + 1)


def _uses_proxy(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return bool(urllib.request.getproxies().get(parsed.scheme)
                and not urllib.request.proxy_bypass(parsed.hostname or ""))


def _trace(event: dict) -> None:
    """Append only diagnostic metadata, never headers, query text or bodies."""
    path = os.environ.get("RADAR_NETWORK_LOG")
    if not path:
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False)
            os.write(descriptor, (line + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        pass  # Diagnostics must not turn a successful public fetch into failure.


def fetch(url: str, *, headers: dict[str, str] | None = None, timeout: float = 40,
          attempts: int = 3, decode=None, attempt_timeout: float = 15,
          direct_fallback: bool = False):
    """Read and validate a complete response with bounded recovery attempts.

Rate-limit/authentication errors and certificate failures are returned directly.
Bad JSON/XML/gzip is retried, never accepted as an empty or partial success.
    timeout bounds the retry budget and socket operations; the deployment runner
    also bounds the whole process for servers that keep streaming slowly.
"""
    if attempts < 1 or timeout <= 0 or attempt_timeout <= 0:
        raise ValueError("attempts and timeout must be positive")
    headers = dict(headers or {})
    parsed = urllib.parse.urlsplit(url)
    proxied = _uses_proxy(url)
    endpoint = {"host": parsed.hostname, "path": parsed.path}
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("page", "per_page"):
        value = query.get(key, [""])[0]
        if value.isdigit():
            endpoint[key] = int(value)
    deadline = time.monotonic() + timeout
    use_curl, direct, last, used = False, False, None, 0
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        used += 1
        started = time.monotonic()
        client = "curl" if use_curl else "urllib"
        route = "without_http_proxy" if direct else "configured_proxy" if proxied else "system_route"
        phase = "request" if use_curl else "open"
        try:
            budget = min(attempt_timeout, remaining)
            if use_curl:
                raw = _curl(url, headers, budget, direct=True) if direct else _curl(url, headers, budget)
            else:
                private = {k: v for k, v in headers.items()
                           if k.lower() in {"authorization", "proxy-authorization"}}
                request = urllib.request.Request(url, headers={k: v for k, v in headers.items() if k not in private})
                for key, value in private.items():
                    request.add_unredirected_header(key, value)
                with urllib.request.urlopen(request, timeout=budget) as response:
                    phase = "read"
                    raw = response.read()
            phase = "decode"
            result = decode(raw) if decode else raw
            _trace({**endpoint, "client": client, "route": route, "phase": phase,
                    "attempt": used, "elapsed": round(time.monotonic() - started, 3),
                    "status": "ok", "error_type": None})
            return result
        except (OSError, http.client.HTTPException, *_BAD_BODY, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                exc = TimeoutError("curl response timed out")
            reason = exc.reason if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception) else exc
            _trace({**endpoint, "client": client, "route": route, "phase": phase,
                    "attempt": used, "elapsed": round(time.monotonic() - started, 3),
                    "status": "error", "error_type": type(reason).__name__,
                    "http_status": exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                    "curl_code": getattr(exc, "curl_code", None)})
            if not _retryable(exc):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            # An HTTP response proves this client/route completed TLS. Keep it
            # for retryable HTTP errors instead of returning to a failed TLS
            # stack or changing routes merely because the attempt count grew.
            if (not isinstance(exc, urllib.error.HTTPError)
                    and parsed.scheme == "https" and shutil.which("curl")):
                if use_curl and direct_fallback and proxied:
                    direct = True
                use_curl = True
            delay = _retry_delay(exc, attempt)
            if delay >= deadline - time.monotonic():
                break
            time.sleep(delay)
    host = parsed.hostname or "discovery source"
    suffix = f" page {endpoint['page']}" if "page" in endpoint else ""
    raise urllib.error.URLError(f"{host}{parsed.path}{suffix}: failed after {used} attempts: {last}") from last
