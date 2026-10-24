#!/usr/bin/env python3
"""Import the two public competitor baselines once, without network or credentials.

Existing cache entries always win.  Validate both sites before writing either one;
publish new entries with an exclusive hard link so a concurrent cache update wins too.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from urllib.parse import unquote, urlsplit
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import sitemaps  # noqa: E402


SITES = {"dreamina": "dreamina.capcut.com", "mediaio": "www.media.io"}
MAX_BYTES = 64 * 1024 * 1024


class SeedError(ValueError):
    """A safe, URL-free diagnostic suitable for an Actions log."""


class RepositoryFiles:
    """Use directory handles to prevent symlinks or path escapes inside the root."""

    def __init__(self, root: Path):
        try:
            self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise SeedError("repository root must be a real directory") from None

    def close(self):
        os.close(self.fd)

    def parent(self, parts: tuple[str, ...], create: bool = False) -> int:
        if not parts or any(p in ("", ".", "..") or "/" in p or "\\" in p for p in parts):
            raise SeedError("invalid repository-relative path")
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, parts: tuple[str, ...], missing_ok: bool = False) -> bytes | None:
        parent = stream = None
        try:
            parent = self.parent(parts)
            stream = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(stream).st_mode):
                raise SeedError("input must be a regular file, never a symlink")
            with os.fdopen(stream, "rb") as handle:
                stream = None
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise SeedError("input exceeds the baseline size limit")
            return data
        except FileNotFoundError:
            if missing_ok:
                return None
            raise SeedError("required input is missing") from None
        except OSError:
            raise SeedError("unsafe or unreadable repository path") from None
        finally:
            if stream is not None:
                os.close(stream)
            if parent is not None:
                os.close(parent)

    def publish(self, parts: tuple[str, ...], data: bytes) -> bool:
        parent = None
        temporary = f".competitor-seed-{uuid.uuid4().hex}.tmp"
        try:
            parent = self.parent(parts, create=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                return False
            os.fsync(parent)
            return True
        except OSError:
            raise SeedError("cannot safely publish baseline") from None
        finally:
            if parent is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)


def decode_snapshot(data: bytes) -> dict:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SeedError("expanded baseline exceeds the size limit")
        result = json.loads(raw)
    except (OSError, EOFError, UnicodeError, ValueError, zlib.error):
        raise SeedError("invalid compressed baseline JSON") from None
    if not isinstance(result, dict):
        raise SeedError("baseline must use the native object schema")
    return result


def validate_snapshot(data: bytes, settings: dict, host: str) -> int:
    """Validate known schema fields; preserve all valid metadata without rewriting it."""
    try:
        fingerprint = sitemaps.scope_fingerprint(settings)
        if settings.get("allowed_host") != host:
            raise ValueError
        sitemaps.normalized_url(settings["index"], host)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SeedError("invalid competitor scope configuration") from None
    prefixes = settings.get("path_prefixes", ["/"])
    snapshot = decode_snapshot(data)
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise SeedError("unsupported native baseline schema version")
    if snapshot.get("baseline_complete") is not True:
        raise SeedError("baseline is incomplete")
    if snapshot.get("scope_fingerprint") != fingerprint:
        raise SeedError("baseline scope fingerprint does not match configuration")

    def check_url(value):
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise SeedError("baseline contains an invalid URL")
        try:
            normalized = sitemaps.normalized_url(value, host)
        except ValueError:
            raise SeedError("baseline URL violates the configured HTTPS host") from None
        path = urlsplit(value).path
        decoded = unquote(path)
        if (normalized != value or "\\" in decoded or any(p in (".", "..") for p in decoded.split("/"))
                or path.endswith((".xml", ".xml.gz")) or not any(
                    not p.rstrip("/") or path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                    for p in prefixes)):
            raise SeedError("baseline URL falls outside the configured page scope")

    urls = snapshot.get("urls")
    if not isinstance(urls, list) or not urls:
        raise SeedError("baseline must contain a nonempty URL list")
    for url in urls:
        check_url(url)
    known = set(urls)
    if len(known) != len(urls):
        raise SeedError("baseline URL list contains duplicates")
    current = snapshot.get("current_urls", urls)
    if not isinstance(current, list):
        raise SeedError("invalid current URL list")
    for url in current:
        check_url(url)
    if len(set(current)) != len(current) or not set(current).issubset(known):
        raise SeedError("current URLs must be unique and belong to the known baseline")
    metadata = snapshot.get("url_metadata")
    if not isinstance(metadata, dict):
        raise SeedError("baseline must contain URL metadata")
    for url, row in metadata.items():
        check_url(url)
        if url not in known or not isinstance(row, dict) or row.get("url") != url:
            raise SeedError("invalid baseline URL metadata")
        if any(key in row and not isinstance(row[key], str) for key in ("first_seen", "last_seen", "lastmod")):
            raise SeedError("invalid baseline URL timestamp metadata")
    coverage = snapshot.get("coverage")
    if coverage is not None and (not isinstance(coverage, dict)
                                 or coverage.get("baseline_complete") is not True):
        raise SeedError("invalid or incomplete baseline coverage")
    return len(urls)


def seed(root: Path) -> list[tuple[str, str, int]]:
    files = RepositoryFiles(root)
    try:
        try:
            config = json.loads(files.read(("config.json",)))
            settings = config["sitemaps"]
            if not isinstance(settings, dict):
                raise ValueError
        except (UnicodeError, ValueError, KeyError, TypeError):
            raise SeedError("invalid sitemap configuration") from None
        planned = []
        for site, host in SITES.items():
            try:
                site_settings = settings[site]
                if not isinstance(site_settings, dict):
                    raise SeedError("missing competitor scope configuration")
                target = ("state", "sitemaps", f"{site}.json.gz")
                existing = files.read(target, missing_ok=True)
                data = existing if existing is not None else files.read(
                    ("bootstrap", "competitor-sitemaps", f"{site}.json.gz"))
                count = validate_snapshot(data, site_settings, host)
                planned.append((site, target, existing, data, count))
            except KeyError:
                raise SeedError(f"{site}: missing competitor scope configuration") from None
            except SeedError as error:
                raise SeedError(f"{site}: {error}") from None
        results = []
        for site, target, existing, data, count in planned:
            if existing is not None:
                status = "existing snapshot kept"
            elif files.publish(target, data):
                status = "imported"
            else:
                # A concurrent collection published first. Never replace its result.
                count = validate_snapshot(files.read(target), settings[site], SITES[site])
                status = "existing snapshot kept"
            results.append((site, status, count))
        return results
    finally:
        files.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: the script's repository)")
    args = parser.parse_args(argv)
    try:
        for site, status, count in seed(args.root):
            print(f"{site}: {status}; {count} known URLs")
    except SeedError as error:
        print(f"competitor seed error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
