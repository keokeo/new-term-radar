"""One-time public competitor seed imports; no network or real cache access."""
import copy
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from radar import sitemaps


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/seed_competitor_sitemaps.py"
SPEC = importlib.util.spec_from_file_location("competitor_seed", SCRIPT)
seeder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seeder)


def compressed(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False).encode(), mtime=0)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("seed import must remain offline")
    monkeypatch.setattr(sitemaps, "fetch", no_network)
    cfg = {"sitemaps": {
        "dreamina": {"index": "https://dreamina.capcut.com/sitemap.xml", "recursive": True,
                     "allowed_host": "dreamina.capcut.com", "path_prefixes": ["/tools", "/resource"],
                     "keep_seen": True, "respect_robots": True},
        "mediaio": {"index": "https://www.media.io/sitemap.xml", "recursive": False,
                    "allowed_host": "www.media.io", "path_prefixes": ["/ai/explore/zone/"],
                    "keep_seen": True, "respect_robots": True},
        "other": {"index": "https://other.example/sitemap.xml"}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    folder = tmp_path / "bootstrap/competitor-sitemaps"
    folder.mkdir(parents=True)
    urls = {"dreamina": "https://dreamina.capcut.com/tools/old-tool",
            "mediaio": "https://www.media.io/ai/explore/zone/old-tool"}
    for site, url in urls.items():
        payload = {"schema_version": 1, "baseline_complete": True,
                   "scope_fingerprint": sitemaps.scope_fingerprint(cfg["sitemaps"][site]),
                   "urls": [url], "current_urls": [url],
                   "url_metadata": {url: {"url": url, "first_seen": "2026-10-20T00:00:00Z",
                                          "last_seen": "2026-10-24T00:00:00Z",
                                          "lastmod": "2026-10-21", "unknown_field": {"retain": True}}},
                   "scan_started_at": "2026-10-24T01:30:00Z",
                   "scan_finished_at": "2026-10-24T01:31:00Z",
                   "coverage": {"baseline_complete": True, "unknown_coverage": 42},
                   "unknown_top_field": ["preserved"]}
        (folder / f"{site}.json.gz").write_bytes(compressed(payload))
    return tmp_path


def seed_path(root, site="dreamina"):
    return root / "bootstrap/competitor-sitemaps" / f"{site}.json.gz"


def target_path(root, site="dreamina"):
    return root / "state/sitemaps" / f"{site}.json.gz"


def change_seed(root, change, site="dreamina"):
    path = seed_path(root, site)
    payload = json.loads(gzip.decompress(path.read_bytes()))
    change(payload)
    path.write_bytes(compressed(payload))


def test_first_import_preserves_all_metadata_and_only_two_sites(repository, capsys):
    untouched = target_path(repository, "other")
    untouched.parent.mkdir(parents=True)
    untouched.write_bytes(b"pre-existing other site snapshot")
    unrelated_seed = seed_path(repository, "other")
    unrelated_seed.write_bytes(b"this is deliberately not a gzip file")
    (repository / ".env").write_text("PRIVATE_VALUE=do-not-read")
    assert seeder.main(["--root", str(repository)]) == 0
    for site in seeder.SITES:
        assert target_path(repository, site).read_bytes() == seed_path(repository, site).read_bytes()
    assert untouched.read_bytes() == b"pre-existing other site snapshot"
    assert sorted(p.name for p in untouched.parent.iterdir()) == [
        "dreamina.json.gz", "mediaio.json.gz", "other.json.gz"]
    output = capsys.readouterr().out
    assert "dreamina: imported; 1 known URLs" in output
    assert "mediaio: imported; 1 known URLs" in output
    assert "https://" not in output
    assert "PRIVATE_VALUE" not in output


def test_repeat_keeps_newer_cache_without_reading_old_seed(repository):
    seeder.seed(repository)
    path = target_path(repository)
    payload = json.loads(gzip.decompress(path.read_bytes()))
    newer = "https://dreamina.capcut.com/tools/newer-tool"
    payload["urls"].append(newer)
    payload["current_urls"] = [newer]
    payload["url_metadata"][newer] = {"url": newer, "first_seen": "2026-10-25T00:00:00Z"}
    payload["scan_finished_at"] = "2026-10-25T00:00:01Z"
    path.write_bytes(compressed(payload))
    newer_bytes = path.read_bytes()
    seed_path(repository).write_bytes(b"the seed may be outdated or unavailable after import")
    results = seeder.seed(repository)
    assert results == [("dreamina", "existing snapshot kept", 2),
                       ("mediaio", "existing snapshot kept", 1)]
    assert path.read_bytes() == newer_bytes


@pytest.mark.parametrize("bad_site", ["dreamina", "mediaio"])
@pytest.mark.parametrize("mutation", [
    lambda p: p.update(scope_fingerprint="0" * 64),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(baseline_complete=False),
    lambda p: p.update(urls={}),
    lambda p: p.update(url_metadata=[]),
    lambda p: p.update(coverage={"baseline_complete": False}),
])
def test_invalid_schema_or_scope_never_imports_either_site(repository, mutation, bad_site):
    change_seed(repository, mutation, bad_site)
    with pytest.raises(seeder.SeedError):
        seeder.seed(repository)
    assert not (repository / "state").exists()


@pytest.mark.parametrize("data", [b"not gzip", gzip.compress(b"[]"), gzip.compress(b"{bad json"),
                                  b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 20])
def test_bad_seed_exits_nonzero_without_writes(repository, data, capsys):
    seed_path(repository, "mediaio").write_bytes(data)
    assert seeder.main(["--root", str(repository)]) == 1
    assert not (repository / "state").exists()
    assert "mediaio:" in capsys.readouterr().err


@pytest.mark.parametrize("url", [
    "https://foreign.example/tools/example",
    "http://dreamina.capcut.com/tools/example",
    "https://user:secret@dreamina.capcut.com/tools/example",
    "https://dreamina.capcut.com/other-scope/example",
    "https://dreamina.capcut.com/tools-other/example",
    "https://dreamina.capcut.com/tools/../private",
    "https://dreamina.capcut.com/tools/%2e%2e/private",
    "https://dreamina.capcut.com/tools/example?unexpected=1",
])
def test_foreign_host_or_out_of_scope_urls_fail_without_writes(repository, url):
    change_seed(repository, lambda p: p.update(urls=[url], current_urls=[url], url_metadata={url: {"url": url}}))
    with pytest.raises(seeder.SeedError):
        seeder.seed(repository)
    assert not (repository / "state").exists()


def test_foreign_metadata_key_and_mismatched_url_are_rejected(repository):
    original = seed_path(repository).read_bytes()
    for bad in ("https://foreign.example/tools/example", "https://dreamina.capcut.com/tools/not-known"):
        seed_path(repository).write_bytes(original)
        change_seed(repository, lambda p: p["url_metadata"].update({bad: {"url": bad}}))
        with pytest.raises(seeder.SeedError):
            seeder.seed(repository)
        assert not (repository / "state").exists()


def test_corrupt_existing_cache_is_never_overwritten(repository, capsys):
    path = target_path(repository)
    path.parent.mkdir(parents=True)
    original = b"corrupted cache must not silently reset"
    path.write_bytes(original)
    assert seeder.main(["--root", str(repository)]) == 1
    assert path.read_bytes() == original
    assert not target_path(repository, "mediaio").exists()
    assert "dreamina:" in capsys.readouterr().err


@pytest.mark.parametrize("relative", ["bootstrap/competitor-sitemaps", "state", "state/sitemaps"])
def test_symlinked_directory_is_rejected(repository, tmp_path_factory, relative):
    outside = tmp_path_factory.mktemp("outside")
    path = repository / relative
    if path.exists():
        moved = repository / "moved-seeds"
        path.rename(moved)
        for child in moved.iterdir():
            (outside / child.name).write_bytes(child.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    with pytest.raises(seeder.SeedError):
        seeder.seed(repository)
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before


@pytest.mark.parametrize("kind", ["seed", "target", "config"])
def test_symlinked_files_are_rejected(repository, tmp_path_factory, kind):
    outside = tmp_path_factory.mktemp("outside") / "value"
    if kind == "seed":
        path = seed_path(repository)
    elif kind == "config":
        path = repository / "config.json"
    else:
        path = target_path(repository)
        path.parent.mkdir(parents=True)
    data = path.read_bytes() if path.exists() else seed_path(repository).read_bytes()
    outside.write_bytes(data)
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    with pytest.raises(seeder.SeedError):
        seeder.seed(repository)
    assert outside.read_bytes() == data
    assert path.is_symlink()


def test_path_components_cannot_escape_repository(repository):
    files = seeder.RepositoryFiles(repository)
    try:
        for parts in (("..", "outside"), ("state/sitemaps", "target"), (".", "target")):
            with pytest.raises(seeder.SeedError):
                files.read(parts)
    finally:
        files.close()


def test_concurrent_new_snapshot_wins(repository, monkeypatch):
    original_publish = seeder.RepositoryFiles.publish
    expected = {}
    def cache_updates_first(files, parts, data):
        updated = copy.deepcopy(json.loads(gzip.decompress(data)))
        updated["scan_finished_at"] = "2026-10-25T00:00:01Z"
        updated["unknown_top_field"] = ["concurrent update"]
        current = compressed(updated)
        assert original_publish(files, parts, current)
        expected[parts[-1]] = current
        return original_publish(files, parts, data)
    monkeypatch.setattr(seeder.RepositoryFiles, "publish", cache_updates_first)
    results = seeder.seed(repository)
    assert all(status == "existing snapshot kept" for _, status, _ in results)
    for site in seeder.SITES:
        assert target_path(repository, site).read_bytes() == expected[f"{site}.json.gz"]
    assert not list((repository / "state/sitemaps").glob(".competitor-seed-*"))
