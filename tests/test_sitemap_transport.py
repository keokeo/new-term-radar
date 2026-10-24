"""Recovery from broken public responses without real network or live state writes."""
from __future__ import annotations

import gzip
import http.client
import json
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

from radar import net, sitemaps, snap

CURL_CLIENT = net._curl


class Response:
    """Only the public context-manager/read API used by the request code."""

    def __init__(self, body=b"", error=None):
        self.body = body
        self.error = error
        self.headers = Message()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self.error:
            raise self.error
        return self.body


@pytest.fixture(autouse=True)
def isolated_requests(monkeypatch):
    """A missing mock must fail locally instead of contacting public services."""
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network request in recovery tests")

    sleeps = []
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(net, "_curl", forbidden)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(net.time, "sleep", sleeps.append)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return sleeps


def tls_eof():
    return urllib.error.URLError(
        ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    )


def xml_urls(*urls):
    return ("<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>").encode()


def xml_index(*urls):
    return ("<sitemapindex>" + "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
            + "</sitemapindex>").encode()


def test_tls_eof_uses_verified_fallback_and_returns_decoded_result(monkeypatch):
    requests, fallbacks = [], []

    def broken(request, timeout):
        requests.append((request.full_url, timeout))
        raise tls_eof()

    def recovered(url, headers, timeout):
        fallbacks.append((url, headers, timeout))
        return b'{"items": []}'

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(net, "_curl", recovered)
    assert net.fetch("https://api.github.com/search/repositories", headers={"Accept": "application/json"},
                     timeout=40, decode=json.loads) == {"items": []}
    assert len(requests) == 1 and len(fallbacks) == 1
    assert fallbacks[0][0] == requests[0][0]
    assert fallbacks[0][1]["Accept"] == "application/json"
    assert 0 < fallbacks[0][2] <= 40


def test_tls_eof_without_curl_retries_with_a_fresh_request(monkeypatch):
    calls = []

    def flaky(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise tls_eof()
        return Response(b"complete")

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert net.fetch("https://example.test/public", attempts=3) == b"complete"
    assert len(calls) == 2


def test_repeated_tls_eof_is_bounded_without_curl(monkeypatch):
    calls = []

    def broken(request, timeout):
        calls.append(timeout)
        raise tls_eof()

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    with pytest.raises(OSError):
        net.fetch("https://example.test/public", attempts=3)
    assert len(calls) == 3
    assert all(0 < timeout <= 40 for timeout in calls)


def test_incomplete_read_discards_partial_body_and_retries(monkeypatch):
    responses = iter([Response(error=http.client.IncompleteRead(b"partial", 8)), Response(b"complete")])
    calls = []

    def open_response(request, timeout):
        calls.append(request.full_url)
        return next(responses)

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    assert net.fetch("https://example.test/public") == b"complete"
    assert len(calls) == 2


@pytest.mark.parametrize("bad_body", [
    gzip.compress(xml_urls("https://example.test/partial"))[:-7],
    b"<urlset><url><loc>https://example.test/partial</loc></url>",
], ids=["truncated-gzip", "truncated-xml"])
def test_sitemap_retries_truncated_body_before_accepting_complete_xml(monkeypatch, bad_body):
    complete = xml_urls("https://example.test/complete")
    responses = iter([Response(bad_body), Response(complete)])
    calls = []

    def open_response(request, timeout):
        calls.append(request.full_url)
        return next(responses)

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    assert sitemaps.fetch("https://example.test/sitemap.xml") == complete.decode()
    assert len(calls) == 2


def test_complete_gzip_sitemap_still_reads_urls(monkeypatch):
    complete = xml_urls("https://example.test/one", "https://example.test/two")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response(gzip.compress(complete)))
    assert sitemaps.site_urls("https://example.test/sitemap.xml", None, 0) == {
        "https://example.test/one", "https://example.test/two",
    }


def test_failed_later_shard_preserves_whole_site_and_other_site_continues(tmp_path, monkeypatch):
    failed_path = tmp_path / "sitemaps/bad.json.gz"
    good_path = tmp_path / "sitemaps/good.json.gz"
    snap.save(failed_path, ["https://bad.test/old"])
    snap.save(good_path, ["https://good.test/old"])
    prior = failed_path.read_bytes()
    calls = []
    fixtures = {
        "https://bad.test/sitemap.xml": xml_index("https://bad.test/1.xml", "https://bad.test/2.xml"),
        "https://bad.test/1.xml": xml_urls("https://bad.test/partial-new"),
        "https://good.test/sitemap.xml": xml_urls("https://good.test/old", "https://good.test/new"),
    }

    def open_response(request, timeout):
        url = request.full_url
        calls.append(url)
        if url == "https://bad.test/2.xml":
            return Response(error=http.client.IncompleteRead(b"<urlset>", 20))
        return Response(fixtures[url])

    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    cfg = {"sitemaps": {
        "bad": {"index": "https://bad.test/sitemap.xml", "shards": r"/\d+\.xml$", "pause": 0},
        "good": {"index": "https://good.test/sitemap.xml", "pause": 0},
    }}
    report = sitemaps.diff(cfg, tmp_path)
    assert report["sites"]["bad"].get("error")
    assert failed_path.read_bytes() == prior
    assert report["sites"]["good"]["new"] == 1
    assert snap.load(good_path) == ["https://good.test/new", "https://good.test/old"]
    assert calls.count("https://bad.test/2.xml") == 3


def test_truncated_xml_never_replaces_site_baseline(tmp_path, monkeypatch):
    path = tmp_path / "sitemaps/site.json.gz"
    snap.save(path, ["https://example.test/old-a", "https://example.test/old-b"])
    prior = path.read_bytes()
    # There is a complete <loc>, so regex-only parsing would incorrectly commit it.
    partial = b"<urlset><url><loc>https://example.test/old-a</loc></url>"
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response(partial))
    report = sitemaps.diff({"sitemaps": {"site": {"index": "https://example.test/sitemap.xml", "pause": 0}}}, tmp_path)
    assert report["sites"]["site"].get("error")
    assert path.read_bytes() == prior
    assert report["terms"] == []








@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_http_permanent_or_rate_limit_error_fails_without_retry_or_fallback(monkeypatch, isolated_requests, status):
    calls = []
    headers = Message()
    headers["Retry-After"] = "2"

    def rejected(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, status, "rejected", headers, None)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    with pytest.raises(urllib.error.HTTPError) as error:
        net.fetch("https://example.test/public", attempts=3)
    assert error.value.code == status
    assert len(calls) == 1 and isolated_requests == []


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "urlerror-wrapped"])
def test_certificate_failure_does_not_retry_or_use_fallback(monkeypatch, isolated_requests, wrapped):
    calls = []

    def untrusted(request, timeout):
        calls.append(request.full_url)
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        raise urllib.error.URLError(error) if wrapped else error

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(urllib.request, "urlopen", untrusted)
    with pytest.raises((ssl.SSLCertVerificationError, urllib.error.URLError)):
        net.fetch("https://example.test/public")
    assert len(calls) == 1 and isolated_requests == []


@pytest.mark.parametrize("status", [408, 500, 503])
def test_temporary_http_error_retries_and_honors_retry_after(monkeypatch, isolated_requests, status):
    calls = []
    headers = Message()
    headers["Retry-After"] = "2"

    def temporary(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, status, "temporary", headers, None)
        return Response(b"recovered")

    monkeypatch.setattr(urllib.request, "urlopen", temporary)
    assert net.fetch("https://example.test/public") == b"recovered"
    assert len(calls) == 2
    assert isolated_requests and isolated_requests[0] >= 2


def test_retry_after_cannot_exceed_total_request_budget(monkeypatch, isolated_requests):
    calls = []
    headers = Message()
    headers["Retry-After"] = "120"

    def unavailable(request, timeout):
        calls.append(timeout)
        raise urllib.error.HTTPError(request.full_url, 503, "temporary", headers, None)

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    with pytest.raises(OSError):
        net.fetch("https://example.test/public", timeout=1, attempts=3)
    assert len(calls) == 1
    assert sum(isolated_requests) <= 1


def test_authorization_is_sent_only_on_initial_urllib_request(monkeypatch):
    def respond(request, timeout):
        assert request.get_header("Authorization") == "Bearer fixture-token"
        assert "Authorization" not in request.headers
        assert request.unredirected_hdrs["Authorization"] == "Bearer fixture-token"
        return Response(b"complete")

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    assert net.fetch("https://api.github.com/public", headers={"Authorization": "Bearer fixture-token"}) == b"complete"


@pytest.mark.parametrize("status,returncode", [(200, 0), (503, 0), (0, 60)])
def test_curl_keeps_verification_and_token_out_of_process_arguments(monkeypatch, status, returncode):
    secret = "fixture-token-not-a-real-credential"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/curl")

    def run(args, *, input, capture_output, text, timeout):
        assert all(secret not in arg for arg in args)
        assert "--insecure" not in args and "-k" not in args
        assert args[args.index("--config") + 1] == "-"
        assert args[args.index("--proto-redir") + 1] == "=https"
        assert secret in input
        Path(args[args.index("--output") + 1]).write_bytes(b"complete")
        Path(args[args.index("--dump-header") + 1]).write_bytes(
            f"HTTP/1.1 {status} Test\r\nRetry-After: 2\r\n\r\n".encode())
        return subprocess.CompletedProcess(args, returncode, stdout=str(status), stderr="")

    monkeypatch.setattr(net.subprocess, "run", run)
    if returncode:
        with pytest.raises(ssl.SSLCertVerificationError):
            CURL_CLIENT("https://api.github.com/public", {"Authorization": "Bearer " + secret}, 5)
    elif status >= 400:
        with pytest.raises(urllib.error.HTTPError) as error:
            CURL_CLIENT("https://api.github.com/public", {"Authorization": "Bearer " + secret}, 5)
        assert error.value.code == status
        assert error.value.headers["Retry-After"] == "2"
    else:
        assert CURL_CLIENT("https://api.github.com/public", {"Authorization": "Bearer " + secret}, 5) == b"complete"
