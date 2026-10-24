"""The free cloud check exercises real scoped scans without advancing daily state."""
import gzip
import json
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

from radar import aisa, notify, sitemaps
from scripts import check_competitor_sitemaps as checker


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "sitemaps").mkdir(parents=True)
    settings, documents = {}, {}
    for site, host in checker.SITES.items():
        settings[site] = {"index": f"https://{host}/sitemap.xml", "allowed_host": host,
                          "path_prefixes": ["/ai-video/"], "recursive": False,
                          "keep_seen": True, "respect_robots": True, "pause": 0}
        old, new = f"https://{host}/ai-video/old", f"https://{host}/ai-video/new"
        snapshot = {"schema_version": 1, "baseline_complete": True,
                    "scope_fingerprint": sitemaps.scope_fingerprint(settings[site]),
                    "urls": [old], "current_urls": [old],
                    "url_metadata": {old: {"url": old, "first_seen": "2026-10-20T00:00:00Z"}},
                    "private_unknown_field": {"keep": "original"}}
        (state / "sitemaps" / f"{site}.json.gz").write_bytes(gzip.compress(json.dumps(snapshot).encode()))
        documents[settings[site]["index"]] = ("<urlset>" + "".join(
            f"<url><loc>{url}</loc><lastmod>2026-10-24</lastmod></url>" for url in [old, new]) + "</urlset>")
    settings["unrelated"] = {"index": "https://must-not-request.test/sitemap.xml"}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sitemaps": settings, "judge": {"model": "must-not-call"}}))
    (state / "sitemaps" / "unrelated.json.gz").write_bytes(b"other source must not change")
    (state / "radar.db").write_bytes(b"database must not change")
    before = {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()}
    calls = []

    def get(url):
        calls.append(url)
        value = documents[url]
        if isinstance(value, Exception):
            raise value
        return value

    def robots(url, **kwargs):
        assert url in {f"https://host/robots.txt".replace("host", host) for host in checker.SITES.values()}
        calls.append(url)
        return b"User-agent: *\nAllow: /\n"

    def forbidden(*args, **kwargs):
        raise AssertionError("paid or email entrypoint invoked")

    monkeypatch.setattr(sitemaps, "fetch", get)
    monkeypatch.setattr(sitemaps.net, "fetch", robots)
    monkeypatch.setattr(sitemaps.time, "sleep", lambda _: None)
    monkeypatch.setattr(aisa, "_post", forbidden)
    monkeypatch.setattr(notify, "send", forbidden)
    return state, config, tmp_path / "competitors-report.json", before, calls, documents


def assert_unchanged(state, before):
    assert {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()} == before


def test_success_scans_exactly_two_and_keeps_daily_delta(setup, monkeypatch):
    state, config, out, before, calls, _ = setup
    actual_diff = sitemaps.diff

    def only_two(cfg, temporary):
        assert set(cfg) == {"sitemaps"}
        assert set(cfg["sitemaps"]) == set(checker.SITES)
        assert temporary != state
        assert sorted(str(path.relative_to(temporary)) for path in temporary.rglob("*") if path.is_file()) == [
            "sitemaps/dreamina.json.gz", "sitemaps/mediaio.json.gz"]
        return actual_diff(cfg, temporary)

    monkeypatch.setattr(sitemaps, "diff", only_two)
    assert checker.main(["--config", str(config), "--state", str(state), "--out", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["status"] == "ok" and result["validation_only"]
    assert result["production_baselines_unchanged"]
    assert not result["paid_calls_enabled"] and not result["mail_enabled"]
    assert result["started_at"] <= result["finished_at"]
    for site, row in result["result"]["sites"].items():
        assert row["new"] == len(row["new_urls"]) == 1
        assert row["new_urls"][0]["url"] == f"https://{checker.SITES[site]}/ai-video/new"
        assert row["new_urls"][0]["first_seen"] == row["scan_started_at"]
        assert row["coverage"]["robots_checked"] and row["coverage"]["baseline_complete"]
        proof = result["source_baselines"][site]
        assert proof["unchanged"] and proof["sha256_before"] == proof["sha256_after"]
    assert len(calls) == 4 and all("must-not-request" not in url for url in calls)
    assert json.loads(out.with_name("competitors-status.json").read_text())["status"] == "ok"
    assert "https://dreamina.capcut.com/ai-video/new" in out.with_suffix(".md").read_text()
    assert_unchanged(state, before)
    # A subsequent production daily diff still sees the URL: the check consumed nothing.
    assert actual_diff({"sitemaps": {site: json.loads(config.read_text())["sitemaps"][site]
                                     for site in checker.SITES}}, state)["sites"]["dreamina"]["new"] == 1


def test_one_site_failure_still_saves_evidence_and_preserves_all_real_state(setup):
    state, config, out, before, _, documents = setup
    documents["https://dreamina.capcut.com/sitemap.xml"] = urllib.error.HTTPError(
        "https://dreamina.capcut.com/sitemap.xml", 503, "temporarily unavailable", {}, None)
    assert checker.main(["--config", str(config), "--state", str(state), "--out", str(out)]) == 1
    result = json.loads(out.read_text())
    assert result["status"] == "failed" and result["production_baselines_unchanged"]
    failed = result["result"]["sites"]["dreamina"]
    assert "503" in failed["error"] and failed["snapshot_updated"] is False
    assert failed["coverage"]["baseline_complete"] is False
    assert result["result"]["sites"]["mediaio"]["new"] == 1
    assert json.loads(out.with_name("competitors-status.json").read_text())["status"] == "failed"
    assert "503" in out.with_suffix(".md").read_text()
    assert_unchanged(state, before)


@pytest.mark.parametrize("problem", ["missing", "fingerprint", "robots", "invalid_json", "missing_config"])
def test_preflight_failure_never_requests_network(setup, problem):
    state, config, out, _, calls, _ = setup
    path = state / "sitemaps/dreamina.json.gz"
    if problem == "missing":
        path.unlink()
    elif problem == "fingerprint":
        snapshot = json.loads(gzip.decompress(path.read_bytes()))
        snapshot["scope_fingerprint"] = "wrong"
        path.write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    elif problem == "robots":
        data = json.loads(config.read_text())
        data["sitemaps"]["dreamina"]["respect_robots"] = False
        config.write_text(json.dumps(data))
    elif problem == "invalid_json":
        config.write_text("{")
    else:
        config.unlink()
    before = {str(path.relative_to(state)): path.read_bytes() for path in state.rglob("*") if path.is_file()}
    result = checker.check(config, state, out)
    assert result["status"] == "failed" and not calls
    assert set(result["result"]["sites"]) == set(checker.SITES)
    assert out.exists() and out.with_name("competitors-status.json").exists()
    assert_unchanged(state, before)


@pytest.mark.parametrize("problem", ["first_run", "scope_changed", "robots", "coverage", "missing_site", "invalid_row"])
def test_bad_success_shapes_do_not_pass(setup, monkeypatch, problem):
    state, config, out, before, _, _ = setup
    actual_diff = sitemaps.diff

    def bad_result(cfg, temporary):
        result = actual_diff(cfg, temporary)
        row = result["sites"]["dreamina"]
        if problem in ("first_run", "scope_changed"):
            row[problem] = True
        elif problem == "robots":
            row["coverage"]["robots_checked"] = False
        elif problem == "coverage":
            row["coverage"]["baseline_complete"] = False
        elif problem == "missing_site":
            del result["sites"]["dreamina"]
        else:
            result["sites"]["dreamina"] = None
        return result

    monkeypatch.setattr(sitemaps, "diff", bad_result)
    result = checker.check(config, state, out)
    assert result["status"] == "failed" and result["production_baselines_unchanged"]
    assert_unchanged(state, before)


def test_reject_output_inside_state_before_any_change(setup):
    state, config, _, before, calls, _ = setup
    with pytest.raises(ValueError, match="outside production state"):
        checker.check(config, state, state / "sitemaps/dreamina.json.gz")
    assert not calls
    assert_unchanged(state, before)


def test_direct_script_imports_from_an_unrelated_working_directory(tmp_path):
    script = Path(checker.__file__).resolve()
    completed = subprocess.run([sys.executable, str(script), "--config", str(tmp_path / "missing.json"),
                                "--state", str(tmp_path / "state"), "--out", str(tmp_path / "result.json")],
                               cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert completed.returncode == 1, completed.stderr
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "failed"
