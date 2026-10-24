"""Complete native competitor scopes; all network and state stay in fixtures."""
import urllib.error

import pytest

from radar import sitemaps, snap


HOST = "example.test"
ROOT = f"https://{HOST}/sitemap.xml"
ARTICLE = f"https://{HOST}/article/sitemap_index.xml"
LEAF = f"https://{HOST}/default/index0.xml"
OLD = f"https://{HOST}/ai-video/old"
NEW = f"https://{HOST}/ai-video/new"


def urlset(*urls):
    return '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        f"<url><loc>{url}</loc><lastmod>2026-10-24</lastmod></url>" for url in urls) + '</urlset>'


def index(*urls):
    return '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        f"<sitemap><loc>{url}</loc></sitemap>" for url in urls) + '</sitemapindex>'


def settings(**kwargs):
    return {"index": ROOT, "allowed_host": HOST, "recursive": True,
            "shards": r"/article/sitemap_index\.xml$|/default/[^/]+\.xml$",
            "path_prefixes": ["/ai-video"], "pause": 0, "max_sitemaps": 24,
            "keep_seen": True, **kwargs}


def network(monkeypatch, fixtures):
    calls = []
    def get(url):
        calls.append(url)
        answer = fixtures[url]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(sitemaps, "fetch", get)
    monkeypatch.setattr(sitemaps.time, "sleep", lambda seconds: None)
    return calls


def seed(tmp_path, cfg, urls=(OLD,), **extra):
    path = tmp_path / "sitemaps/native.json.gz"
    snap.save(path, {"schema_version": 1, "scope_fingerprint": sitemaps.scope_fingerprint(cfg),
                     "baseline_complete": True, "urls": list(urls), "current_urls": list(urls),
                     "url_metadata": {u: {"url": u, "first_seen": "2026-10-20T00:00:00Z",
                                           "last_seen": "2026-10-23T00:00:00Z",
                                           "lastmod": "old-lastmod", "unknown_url_field": 42} for u in urls},
                     "unknown_top_field": {"keep": True}, **extra})
    return path


def run(tmp_path, cfg):
    return sitemaps.diff({"sitemaps": {"native": cfg}}, tmp_path)["sites"]["native"]


def test_nested_indexes_only_count_english_pages_and_not_xml_urls(monkeypatch):
    cfg = settings()
    foreign = "https://elsewhere.test/ai-video/no"
    calls = network(monkeypatch, {ROOT: index(ARTICLE, f"https://{HOST}/fr-fr/index0.xml"),
                                  ARTICLE: index(LEAF),
                                  LEAF: urlset(OLD, NEW + "/?ignored=yes#fragment", foreign,
                                               f"https://{HOST}/fr/ai-video/no", f"https://{HOST}/ai-videos/no",
                                               f"https://{HOST}/ai-video/sitemap.xml")})
    urls, coverage = sitemaps.scoped_site_urls(cfg)
    assert urls == {OLD, NEW}
    assert calls == [ROOT, ARTICLE, LEAF]
    assert coverage["baseline_complete"] and coverage["xml_requested"] == 3
    assert coverage["rejected_url_count"] == 1
    assert coverage["skipped_shards"] == [f"https://{HOST}/fr-fr/index0.xml"]
    assert coverage["_url_metadata"][NEW]["lastmod"] == "2026-10-24"


def test_mediaio_plain_urlset_filters_by_path_segment(monkeypatch):
    cfg = settings(shards=None, path_prefixes=["/ai/explore/zone/"])
    good = f"https://{HOST}/ai/explore/zone/word"
    network(monkeypatch, {ROOT: urlset(good + "/", f"https://{HOST}/ai/explore/zones/no",
                                      f"https://{HOST}/fr/ai/explore/zone/no")})
    urls, coverage = sitemaps.scoped_site_urls(cfg)
    assert urls == {good} and coverage["xml_requested"] == 1


@pytest.mark.parametrize("problem", ["missing", "empty", "malformed", "no_match", "cycle", "limit", "foreign_child"])
def test_incomplete_selected_scope_never_replaces_baseline(tmp_path, monkeypatch, problem):
    cfg = settings()
    path = seed(tmp_path, cfg)
    original = path.read_bytes()
    second = f"https://{HOST}/default/index1.xml"
    fixtures = {ROOT: index(ARTICLE), ARTICLE: index(LEAF, second), LEAF: urlset(OLD, NEW),
                second: OSError("unavailable second shard")}
    if problem == "empty":
        fixtures[second] = urlset()
    elif problem == "malformed":
        fixtures[second] = "<urlset><url><loc>https://example.test/ai-video/half</loc>"
    elif problem == "no_match":
        fixtures[ARTICLE] = index(f"https://{HOST}/de/index0.xml")
    elif problem == "cycle":
        fixtures[LEAF] = index(ARTICLE)
    elif problem == "limit":
        cfg["max_sitemaps"] = 2
    elif problem == "foreign_child":
        fixtures[ARTICLE] = index("https://foreign.test/default/index0.xml")
    network(monkeypatch, fixtures)
    report = run(tmp_path, cfg)
    assert "error" in report and report["snapshot_updated"] is False
    assert report["coverage"]["baseline_complete"] is False
    assert report["scan_started_at"] and report["scan_finished_at"]
    assert path.read_bytes() == original


def test_native_migration_snapshot_keeps_metadata_unknown_fields_and_seen_union(tmp_path, monkeypatch):
    cfg = settings(shards=None)
    hidden = f"https://{HOST}/ai-video/temporarily-removed"
    path = seed(tmp_path, cfg, (OLD, hidden))
    network(monkeypatch, {ROOT: urlset(OLD + "/?source=x", NEW)})
    result = run(tmp_path, cfg)
    saved = snap.load(path)
    assert not result["first_run"] and result["new"] == 1 and result["sample"] == [NEW]
    assert result["new_urls"][0]["url"] == NEW
    assert result["new_urls"][0]["first_seen"] == result["scan_started_at"]
    assert saved["urls"] == sorted([OLD, NEW, hidden]) and saved["current_urls"] == sorted([OLD, NEW])
    assert saved["url_metadata"][OLD]["first_seen"] == "2026-10-20T00:00:00Z"
    assert saved["url_metadata"][OLD]["unknown_url_field"] == 42
    assert saved["url_metadata"][OLD]["lastmod"] == "2026-10-24"
    assert saved["url_metadata"][hidden]["last_seen"] == "2026-10-23T00:00:00Z"
    assert saved["unknown_top_field"] == {"keep": True}
    network(monkeypatch, {ROOT: urlset(OLD, NEW, hidden)})
    assert run(tmp_path, cfg)["new"] == 0


@pytest.mark.parametrize("stored_kind", ["legacy_list", "changed_scope", "incomplete"])
def test_unverified_or_changed_scope_only_builds_baseline(tmp_path, monkeypatch, stored_kind):
    cfg = settings(shards=None)
    path = seed(tmp_path, cfg)
    if stored_kind == "legacy_list":
        snap.save(path, [OLD])
    else:
        saved = snap.load(path)
        saved["scope_fingerprint" if stored_kind == "changed_scope" else "baseline_complete"] = (
            "different-scope" if stored_kind == "changed_scope" else False)
        snap.save(path, saved)
    network(monkeypatch, {ROOT: urlset(OLD, NEW)})
    result = run(tmp_path, cfg)
    assert result["first_run"] and result["new"] == 0 and not result["new_urls"]
    assert result["scope_changed"]
    assert snap.load(path)["scope_fingerprint"] == sitemaps.scope_fingerprint(cfg)


def test_scope_fingerprint_ignores_timing_and_request_limits():
    cfg = settings()
    fingerprint = sitemaps.scope_fingerprint(cfg)
    assert fingerprint == sitemaps.scope_fingerprint({**cfg, "pause": 12, "max_sitemaps": 50})
    assert fingerprint == sitemaps.scope_fingerprint({**cfg, "path_prefixes": ["/ai-video/"]})
    assert fingerprint != sitemaps.scope_fingerprint({**cfg, "path_prefixes": ["/ai-image"]})
    assert fingerprint != sitemaps.scope_fingerprint({**cfg, "recursive": False})


def test_atomic_write_failure_keeps_existing_snapshot(tmp_path, monkeypatch):
    cfg = settings(shards=None)
    path = seed(tmp_path, cfg)
    before = path.read_bytes()
    network(monkeypatch, {ROOT: urlset(OLD, NEW)})
    monkeypatch.setattr(sitemaps.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    assert "disk full" in run(tmp_path, cfg)["error"]
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".sitemap-*"))


def test_respect_robots_denies_sitemap_without_reading_it(tmp_path, monkeypatch):
    cfg = settings(shards=None, respect_robots=True)
    path = seed(tmp_path, cfg)
    before = path.read_bytes()
    monkeypatch.setattr(sitemaps.net, "fetch", lambda *a, **k: b"User-agent: *\nDisallow: /sitemap.xml\n")
    monkeypatch.setattr(sitemaps, "fetch", lambda *a: pytest.fail("robots-denied sitemap must not be read"))
    result = run(tmp_path, cfg)
    assert "robots.txt disallows" in result["error"] and path.read_bytes() == before


def test_respect_robots_error_does_not_try_sitemap_or_replace_baseline(tmp_path, monkeypatch):
    cfg = settings(shards=None, respect_robots=True)
    path = seed(tmp_path, cfg)
    before = path.read_bytes()
    calls = []
    def robots(*args, **kwargs):
        calls.append(args[0])
        raise urllib.error.HTTPError(args[0], 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(sitemaps.net, "fetch", robots)
    monkeypatch.setattr(sitemaps, "fetch", lambda *a: pytest.fail("unavailable rules must defer sitemap"))
    assert "429" in run(tmp_path, cfg)["error"]
    assert calls == [f"https://{HOST}/robots.txt"] and path.read_bytes() == before


def test_legacy_four_argument_api_and_list_snapshot_are_unchanged(tmp_path, monkeypatch):
    network(monkeypatch, {ROOT: urlset(OLD)})
    assert sitemaps.site_urls(ROOT, None, 0, False) == {OLD}
    cfg = {"index": ROOT, "pause": 0}
    assert run(tmp_path, cfg)["first_run"]
    path = tmp_path / "sitemaps/native.json.gz"
    assert snap.load(path) == [OLD]
    network(monkeypatch, {ROOT: urlset(OLD, NEW)})
    report = run(tmp_path, cfg)
    assert report == {"urls": 2, "new": 1, "first_run": False, "sample": [NEW]}
