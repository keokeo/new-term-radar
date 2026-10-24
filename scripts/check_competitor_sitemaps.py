#!/usr/bin/env python3
"""Check the two public competitor scopes without consuming the daily URL delta.

The workflow seeds production state first. This check copies only those two
snapshots into a temporary directory, then scans there. It never invokes the
radar CLI, a model, a paid API, or mail delivery.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import sitemaps  # noqa: E402
from scripts.seed_competitor_sitemaps import SITES, SeedError, validate_snapshot  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_snapshot(path: Path) -> bytes:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise SeedError("required baseline is missing or is not a regular file")
    return path.read_bytes()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".competitors-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _markdown(report: dict) -> str:
    lines = ["# Dreamina / Media.io 免费采集验收", "",
             f"状态：{report['status']}；开始：{report['started_at']}；结束：{report['finished_at']}", "",
             "仅扫描配置的英文 sitemap 路径。使用临时快照，正式基线不前移，新增 URL 留给下一次日报。",
             "本次不调用 AIsa、其他付费接口或邮件发送。", ""]
    for site, row in report["result"]["sites"].items():
        lines.append(f"## {site}")
        lines.append("")
        if row.get("error"):
            lines.append(f"未通过：{row['error']}")
        else:
            lines.append(f"扫描 URL：{row.get('urls', 0)}；相对基线新增 URL：{row.get('new', 0)}。")
            lines.append(f"扫描时间：{row.get('scan_started_at')} → {row.get('scan_finished_at')}")
            lines.append(f"配置路径：{', '.join(report['configured_scopes'].get(site, {}).get('path_prefixes', []))}")
            for item in row.get("new_urls", []):
                lines.append(f"- <{item['url']}> （first_seen：{item.get('first_seen', 'unknown')}；lastmod：{item.get('lastmod', 'unknown')}）")
        lines.append("")
    if report["errors"]:
        lines.append("校验问题：" + "; ".join(report["errors"]))
        lines.append("")
    lines.append(f"正式两站基线哈希保持一致：{report['production_baselines_unchanged']}")
    return "\n".join(lines) + "\n"


def check(config_path: Path, state: Path, out: Path) -> dict:
    """Persist complete evidence even when preflight or one site's scan fails."""
    state = state.resolve()
    paths = (out.resolve(), out.with_name("competitors-status.json").resolve(), out.with_suffix(".md").resolve())
    if len(set(paths)) != 3 or any(path.is_relative_to(state) for path in paths):
        raise ValueError("report outputs must be distinct files outside production state")
    report = {"schema_version": 1, "command": "competitors", "status": "failed",
              "validation_only": True, "paid_calls_enabled": False, "mail_enabled": False,
              "started_at": _now(), "finished_at": None, "configured_scopes": {},
              "result": {"sites": {}}, "source_baselines": {}, "errors": []}
    baseline_bytes = {}
    selected = {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        settings = raw["sitemaps"]
        if not isinstance(settings, dict):
            raise ValueError
        for site, host in SITES.items():
            try:
                config = settings[site]
                if not isinstance(config, dict) or config.get("respect_robots") is not True:
                    raise SeedError("competitor configuration must enable robots checks")
                selected[site] = copy.deepcopy(config)
                report["configured_scopes"][site] = {
                    key: config.get(key) for key in ("index", "allowed_host", "path_prefixes", "shards", "recursive")}
                data = _read_snapshot(state / "sitemaps" / f"{site}.json.gz")
                baseline_bytes[site] = data
                report["source_baselines"][site] = {"sha256_before": _digest(data)}
                validate_snapshot(data, config, host)
            except (KeyError, OSError, SeedError) as error:
                message = str(error) if isinstance(error, SeedError) else "missing or unreadable competitor configuration/baseline"
                report["result"]["sites"][site] = {"error": message, "snapshot_updated": False}
                report["errors"].append(f"{site}: {message}")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        report["errors"].append("missing or invalid JSON sitemap configuration")

    if not report["errors"]:
        with tempfile.TemporaryDirectory(prefix="radar-competitor-check-") as directory:
            temporary_state = Path(directory)
            (temporary_state / "sitemaps").mkdir()
            for site, data in baseline_bytes.items():
                (temporary_state / "sitemaps" / f"{site}.json.gz").write_bytes(data)
            try:
                result = sitemaps.diff({"sitemaps": selected}, temporary_state)
                if not isinstance(result, dict) or not isinstance(result.get("sites"), dict):
                    raise ValueError("invalid scan result")
                report["result"] = result
            except Exception as error:  # noqa: BLE001 -- retain status/artifacts for an unexpected failure
                report["errors"].append(f"scan failed: {type(error).__name__}")
    rows = report["result"]["sites"]
    if set(rows) - set(SITES):
        report["errors"].append("scan returned unexpected sites")
    for site, row in list(rows.items()):
        if not isinstance(row, dict):
            rows[site] = {"error": "invalid source scan result", "snapshot_updated": False}
    for site in SITES:
        row = rows.setdefault(site, {"error": "scan not completed", "snapshot_updated": False})
        problems = []
        if row.get("error"):
            problems.append("source error")
        if row.get("snapshot_updated") is not True:
            problems.append("temporary snapshot not updated")
        coverage = row.get("coverage", {})
        if not isinstance(coverage, dict) or coverage.get("baseline_complete") is not True:
            problems.append("configured coverage incomplete")
        if not isinstance(coverage, dict) or coverage.get("robots_checked") is not True:
            problems.append("robots check missing")
        if row.get("first_run") is not False or row.get("scope_changed") is not False:
            problems.append("original baseline was not reused")
        if not row.get("scan_started_at") or not row.get("scan_finished_at"):
            problems.append("source scan timestamps missing")
        if problems:
            report["errors"].append(f"{site}: {', '.join(problems)}")
        proof = report["source_baselines"].setdefault(site, {"sha256_before": None})
        try:
            proof["sha256_after"] = _digest(_read_snapshot(state / "sitemaps" / f"{site}.json.gz"))
        except (OSError, SeedError):
            proof["sha256_after"] = None
        proof["unchanged"] = proof["sha256_before"] is not None and proof["sha256_before"] == proof["sha256_after"]
    report["production_baselines_unchanged"] = all(row["unchanged"] for row in report["source_baselines"].values())
    if not report["production_baselines_unchanged"]:
        report["errors"].append("production baseline missing or changed during validation")
    report["finished_at"] = _now()
    report["status"] = "failed" if report["errors"] else "ok"
    status = {key: value for key, value in report.items() if key != "result"}
    status["sites"] = {site: {key: value for key, value in row.items() if key not in ("new_urls", "sample")}
                       for site, row in rows.items()}
    _write(paths[0], json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write(paths[1], json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    _write(paths[2], _markdown(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--out", type=Path, default=Path("competitors-report.json"))
    args = parser.parse_args(argv)
    try:
        report = check(args.config, args.state, args.out)
    except (OSError, ValueError) as error:
        print(f"competitor check could not save evidence: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"competitors: {report['status']}; production baselines unchanged: {report['production_baselines_unchanged']}")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
