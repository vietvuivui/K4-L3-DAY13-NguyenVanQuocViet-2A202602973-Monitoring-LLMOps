from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts.dashboard import build_payload

CONFIG = yaml.safe_load(Path("config/dashboard.yaml").read_text(encoding="utf-8"))
NOW = datetime(2026, 9, 29, 10, 0, 30, tzinfo=timezone.utc)


def _rec(minutes_ago: float, **fields) -> dict:
    return {"_ts": NOW - timedelta(minutes=minutes_ago), **fields}


def _ok(minutes_ago: float, latency_ms: int) -> list[dict]:
    return [
        _rec(minutes_ago, event="request_received"),
        _rec(
            minutes_ago,
            event="response_sent",
            latency_ms=latency_ms,
            ttft_ms=50,
            tokens_in=30,
            tokens_out=100,
            cost_usd=0.0016,
            quality_score=0.9,
            tool_success=True,
        ),
    ]


def test_dashboard_has_six_contract_panels_and_ignores_old_records() -> None:
    records = _ok(1, 400) + _ok(2, 600) + _ok(90, 99_999)  # bản ghi 90 phút trước nằm ngoài cửa sổ
    payload = build_payload(CONFIG, records, now=NOW)

    assert [p["id"] for p in payload["panels"]] == [p["id"] for p in CONFIG["dashboard"]["panels"]]
    assert payload["time_range_minutes"] == 60
    assert len(payload["data"]["labels"]) == 60
    latency = payload["data"]["latency"]["stats"]
    assert latency["n"] == 2
    assert latency["p99"] == 600
    assert latency["ttft_p95"] == 50
    assert payload["data"]["tokens"]["stats"] == {"tokens_in": 60, "tokens_out": 200}


def test_errors_panel_reports_error_rate_breakdown_and_retrieval_success() -> None:
    records = _ok(1, 400) + [
        _rec(1, event="request_received"),
        _rec(1, event="request_failed", error_type="RuntimeError", tool_name="retrieval", tool_success=False),
    ]
    payload = build_payload(CONFIG, records, now=NOW)
    errors = payload["data"]["errors"]["stats"]

    assert errors["error_rate_pct"] == 50.0
    assert errors["tool_success_rate_pct"] == 50.0
    assert errors["count_by_value"] == {"RuntimeError": 1}
    status = {p["id"]: p["status"] for p in payload["panels"]}
    assert status["errors"] == "BREACH"
    assert status["cost"] == "OK"


def test_empty_window_reports_no_data() -> None:
    payload = build_payload(CONFIG, [], now=NOW)
    status = {p["id"]: p["status"] for p in payload["panels"]}
    # percentile/tỷ lệ/mean không có mẫu -> NO_DATA; traffic = 0 req/phút thì vi phạm threshold >= 1
    assert status["latency"] == status["errors"] == status["quality"] == "NO_DATA"
    assert status["traffic"] == "BREACH"
