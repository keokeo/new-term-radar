"""Signal ③: pages other sites added since the last run.

Reads the public sitemaps listed in config.json ("sitemaps"), keeps the last run's URLs
in a snapshot, and turns the new URLs into terms: a word that three or more new URLs
share, and that the site's older URLs rarely carried, is a surge.

    "sitemaps": {
      "glama":   {"index": "https://glama.ai/sitemap.xml", "shards": "/mcp-servers/\\\\d+\\\\.xml$", "pause": 2},
      "lobehub": {"index": "https://lobehub.com/sitemap/mcp.xml", "pause": 5, "strip_locale": true}
    }

`index` is a sitemap index when `shards` (a regex) picks the sub-sitemaps to read, or a
plain sitemap when `shards` is left out. `pause` is the seconds between requests to that
site: keep each site's robots.txt and crawl-delay. `strip_locale` drops a leading
/en/ or /zh-CN/ so one new entry in 17 languages counts once.

Scoped entries opt in with ``recursive``/``allowed_host``/``path_prefixes``.
They validate every selected nested XML before publishing a scope-identified
snapshot. ``keep_seen`` retains URL history, and ``respect_robots`` checks the
host's rules before any XML request. Existing unscoped entries retain their API.

A site that fails is skipped for the day and keeps its snapshot, since the URLs it
lacks would come back tomorrow as "new".
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from . import UA, net, snap
from .terms import by_source, surges

TIMEOUT = 40
MIN_URLS = 3
TOP = 25
DEFAULT_PAUSE = 3
# A leading path segment that is a language code (/de/, /zh-CN/, /pt_BR/). Only real codes: a slug
# like /ai-blog/ or /rf-detr/ is a page, not a locale.
LANGS = ("en|zh|tw|ja|ko|fr|de|es|pt|it|ru|ar|tr|vi|th|id|nl|pl|sv|da|fi|no|nb|cs|hu|ro|uk|el|he|hi|ms|fa|bn|"
         "ur|ta|te|sk|bg|hr|sr|sl|lt|lv|et|ca|tl|fil|sw")
_LOCALE = rf"^(https?://[^/]+)/(?:{LANGS})(?:[-_][A-Za-z]{{2,4}})?(?=/)"
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)\s*(?:\]\]>)?\s*</loc>")


def fetch(url: str) -> str:
    def decode(body: bytes) -> str:
        text = (gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body).decode("utf-8")
        document = ET.fromstring(text)
        if document.tag.rsplit("}", 1)[-1] not in {"sitemapindex", "urlset"}:
            raise ValueError("response is not a sitemap XML document")
        return text

    return net.fetch(url.replace("&amp;", "&"),
                     headers={"User-Agent": UA, "Accept-Encoding": "gzip"},
                     timeout=TIMEOUT, direct_fallback=True, decode=decode)


def site_urls(index: str, shards: str | None, pause: float, strip_locale: bool = False) -> set[str]:
    files = [index] if not shards else \
        [u for u in _LOC.findall(fetch(index)) if re.search(shards, u.replace("&amp;", "&"))]
    urls: set[str] = set()
    for i, u in enumerate(files):
        if i or shards:
            time.sleep(pause)
        urls.update(x.replace("&amp;", "&") for x in _LOC.findall(fetch(u)))
    return {re.sub(_LOCALE, r"\1", u) for u in urls} if strip_locale else urls


_SCOPE_KEYS = {"recursive", "allowed_host", "path_prefixes", "max_sitemaps", "keep_seen", "respect_robots"}


def _scoped(settings: dict) -> bool:
    return bool(_SCOPE_KEYS.intersection(settings))


def normalized_url(value: str, host: str) -> str:
    """Match the archived watch's HTTPS, host and trailing-slash normalization."""
    parsed = urlsplit(value.strip().replace("&amp;", "&"))
    if (parsed.scheme.lower() != "https" or parsed.hostname != host
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError("URL must use HTTPS and the configured allowed_host")
    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))


def _scope(settings: dict) -> dict:
    host = settings.get("allowed_host") or urlsplit(settings["index"]).hostname
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host.lower()):
        raise ValueError("invalid allowed_host")
    host = host.lower()
    prefixes = settings.get("path_prefixes", ["/"])
    if not isinstance(prefixes, list) or not prefixes or any(
            not isinstance(p, str) or not p.startswith("/") or p.startswith("//") or "?" in p or "#" in p
            for p in prefixes):
        raise ValueError("path_prefixes must be nonempty absolute path segments")
    shards = settings.get("shards")
    if shards is not None and not isinstance(shards, str):
        raise ValueError("shards must be a regex string")
    if shards:
        re.compile(shards)
    return {"index": normalized_url(settings["index"], host), "allowed_host": host,
            "shards": shards or None, "path_prefixes": sorted({p.rstrip("/") for p in prefixes}),
            "recursive": bool(settings.get("recursive")), "strip_locale": bool(settings.get("strip_locale"))}


def scope_fingerprint(settings: dict) -> str:
    """Stable scope identity; timing and request limits do not change the baseline."""
    encoded = json.dumps(_scope(settings), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _child_text(element, name: str) -> str:
    return next(((child.text or "").strip() for child in element
                 if child.tag.rsplit("}", 1)[-1] == name), "")


def scoped_site_urls(settings: dict) -> tuple[set[str], dict]:
    """Read a bounded, complete configured scope; never return sitemap-index URLs as pages.

    The optional ``_url_metadata`` result is consumed by diff, not emitted as coverage.
    Requests keep the existing validated transport and never fetch article bodies.
    """
    scope = _scope(settings)
    limit = settings.get("max_sitemaps", 24)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("max_sitemaps must be an integer between 1 and 1000")
    pause = float(settings.get("pause", DEFAULT_PAUSE))
    if pause < 0:
        raise ValueError("pause must not be negative")
    host = scope["allowed_host"]
    robots = None
    requested = 0
    if settings.get("respect_robots"):
        robots_url = f"https://{host}/robots.txt"
        body = net.fetch(robots_url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                         direct_fallback=True).decode("utf-8-sig")
        if "<html" in body.lower() or "<!doctype html" in body.lower():
            raise ValueError("robots.txt returned HTML instead of crawling rules")
        robots = RobotFileParser(robots_url)
        robots.parse(body.splitlines())
        delay = robots.crawl_delay(UA)
        if delay is not None:
            pause = max(pause, delay)
        requested = 1
    pages, visited, active, files, skipped, metadata = set(), set(), set(), [], [], {}
    rejected = 0

    def walk(url: str):
        nonlocal requested, rejected
        url = normalized_url(url, host)
        if url in active:
            raise ValueError("cyclic sitemap index")
        if url in visited:
            return
        if len(visited) >= limit:
            raise ValueError("max_sitemaps exceeded; baseline retained")
        if robots is not None and not robots.can_fetch(UA, url):
            raise ValueError("robots.txt disallows configured sitemap")
        if requested:
            time.sleep(pause)
        requested += 1
        document = ET.fromstring(fetch(url))
        visited.add(url)
        active.add(url)
        kind = document.tag.rsplit("}", 1)[-1]
        if kind == "sitemapindex":
            if not scope["recursive"]:
                raise ValueError("sitemapindex requires recursive=true for a scoped site")
            children = []
            for item in document:
                if item.tag.rsplit("}", 1)[-1] != "sitemap":
                    continue
                loc = _child_text(item, "loc")
                if not loc:
                    raise ValueError("sitemap index entry has no loc")
                if not scope["shards"] or re.search(scope["shards"], loc.replace("&amp;", "&")):
                    children.append(normalized_url(loc, host))
                else:
                    skipped.append(loc)
            if not children:
                raise ValueError("sitemap index has no selected children")
            files.append({"url": url, "kind": kind, "selected_children": len(children)})
            for child in children:
                walk(child)
        elif kind == "urlset":
            raw_count = included = 0
            for item in document:
                if item.tag.rsplit("}", 1)[-1] != "url":
                    continue
                raw_count += 1
                loc = _child_text(item, "loc")
                if not loc:
                    raise ValueError("urlset entry has no loc")
                try:
                    value = normalized_url(loc, host)
                except ValueError:
                    rejected += 1
                    continue
                path = urlsplit(value).path
                if path.endswith((".xml", ".xml.gz")) or not any(
                        not prefix or path == prefix or path.startswith(prefix + "/")
                        for prefix in scope["path_prefixes"]):
                    continue
                if scope["strip_locale"]:
                    value = re.sub(_LOCALE, r"\1", value)
                pages.add(value)
                included += 1
                row = metadata.setdefault(value, {"url": value})
                lastmod = _child_text(item, "lastmod")
                if lastmod:
                    row["lastmod"] = lastmod
            if not raw_count:
                raise ValueError("empty urlset; baseline retained")
            files.append({"url": url, "kind": kind, "raw_url_count": raw_count, "included_url_count": included})
        else:
            raise ValueError("response is not a sitemap XML document")
        active.remove(url)

    walk(scope["index"])
    if not pages:
        raise ValueError("configured sitemap scope contains no URLs")
    return pages, {"scope": "configured_shards_and_paths", "whole_site_claimed": False,
                   "baseline_complete": True, "path_prefixes": scope["path_prefixes"],
                   "shards": scope["shards"], "xml_requested": len(visited), "xml_limit": limit,
                   "successful_files": files, "skipped_shards": skipped, "rejected_url_count": rejected,
                   "robots_checked": robots is not None, "_url_metadata": metadata}


def _save_scoped(path: Path, data: dict) -> None:
    """Publish a scoped baseline atomically after every selected XML succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".sitemap-", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(gzip.compress(json.dumps(data, ensure_ascii=False).encode()))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def slug_text(url: str) -> str:
    """The words a URL carries: its path after the domain, without ids or file suffixes."""
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    path = re.sub(r"\b[0-9a-f]{8,}\b|\.(html|xml)$", " ", path)
    return re.sub(r"[/_\-]+", " ", path)


def diff(cfg: dict, state: Path) -> dict:
    """New URLs per site against the stored snapshot, then the terms that surge in them."""
    if not cfg["sitemaps"]:
        return {"skipped": True}
    report, recent, baseline = {}, [], []
    for i, (site, s) in enumerate(cfg["sitemaps"].items()):
        path = state / "sitemaps" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', site)}.json.gz"
        scoped = _scoped(s)
        started = datetime.now(timezone.utc).isoformat()
        pause = s.get("pause", DEFAULT_PAUSE)
        try:
            stored = snap.load(path)
            if i:
                time.sleep(pause)   # several entries can point at one host
            if scoped:
                fingerprint = scope_fingerprint(s)
                compatible = (isinstance(stored, dict) and stored.get("baseline_complete") is True
                              and stored.get("scope_fingerprint") == fingerprint)
                if isinstance(stored, dict) and not isinstance(stored.get("urls"), (list, dict)):
                    raise ValueError("scoped sitemap baseline has no valid URLs")
                old = set(stored["urls"]) if compatible else None
                now, coverage = scoped_site_urls(s)
                observed = coverage.pop("_url_metadata", {})
                metadata = dict(stored.get("url_metadata", {})) if isinstance(stored, dict) else {}
                finished = datetime.now(timezone.utc).isoformat()
                for url in now:
                    item = dict(metadata.get(url, {}))
                    item.update(observed.get(url, {}))
                    item.setdefault("url", url)
                    item.setdefault("first_seen", started)
                    item["last_seen"] = started
                    metadata[url] = item
                new = sorted(now - old) if old is not None else []
                seen = now | (old or set()) if s.get("keep_seen") else now
                saved = dict(stored) if isinstance(stored, dict) else {}
                saved.update(schema_version=1, scope_fingerprint=fingerprint, baseline_complete=True,
                             urls=sorted(seen), current_urls=sorted(now), url_metadata=metadata,
                             scanned_at=finished, scan_started_at=started, scan_finished_at=finished,
                             coverage=coverage)
                _save_scoped(path, saved)
                report[site] = {"urls": len(now), "new": len(new), "first_run": old is None,
                                "sample": new[:8], "new_urls": [dict(metadata[u]) for u in new],
                                "known_urls": len(seen), "coverage": coverage, "snapshot_updated": True,
                                "scope_changed": stored is not None and not compatible,
                                "scan_started_at": started, "scan_finished_at": finished}
            else:
                old = set(stored) if stored is not None else None
                now = site_urls(s["index"], s.get("shards"), pause, bool(s.get("strip_locale")))
                if not now:
                    raise ValueError("no URLs read")
                new = sorted(now - old) if old is not None else []
                snap.save(path, sorted(now))
                report[site] = {"urls": len(now), "new": len(new), "first_run": old is None, "sample": new[:8]}
        except Exception as exc:  # noqa: BLE001 — one site down must not stop the others
            report[site] = {"error": str(exc)[:120]}
            if scoped:
                report[site].update(coverage={"scope": "configured_shards_and_paths", "whole_site_claimed": False,
                                              "baseline_complete": False}, snapshot_updated=False,
                                    scan_started_at=started, scan_finished_at=datetime.now(timezone.utc).isoformat())
            continue
        recent += [(u, slug_text(u)) for u in new]
        baseline += [(u, slug_text(u)) for u in (old or set())]
    ratio = len(recent) / max(len(baseline), 1)
    return {"sites": report, "terms": surges(by_source(recent), by_source(baseline), ratio, MIN_URLS, TOP)}
