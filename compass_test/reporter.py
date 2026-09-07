"""Persists/reads TestRunReport as JSON — the exact contract
visualization/server.py's /api/test-runs endpoints (and the frontend's Test
Results tab) read. One file per run (<runId>.json) plus latest.json, a copy
of the most recent run for a cheap "what's the current state" read."""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .models import TestRunReport


def _run_path(run_id: str) -> Path:
    return config.REPORTS_DIR / f"{run_id}.json"


def save_report(report: TestRunReport) -> Path:
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _run_path(report.runId)
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    path.write_text(payload, encoding="utf-8")
    (config.REPORTS_DIR / "latest.json").write_text(payload, encoding="utf-8")
    return path


def load_report(run_id: str) -> TestRunReport:
    path = _run_path(run_id)
    return TestRunReport.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_reports() -> list[TestRunReport]:
    if not config.REPORTS_DIR.exists():
        return []
    reports = []
    for path in sorted(config.REPORTS_DIR.glob("*.json")):
        if path.name == "latest.json":
            continue
        reports.append(TestRunReport.from_dict(json.loads(path.read_text(encoding="utf-8"))))
    reports.sort(key=lambda r: r.createdAt, reverse=True)
    return reports
