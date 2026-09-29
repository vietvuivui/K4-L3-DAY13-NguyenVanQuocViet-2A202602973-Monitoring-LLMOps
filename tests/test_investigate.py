from __future__ import annotations

from scripts.investigate import detect, pick_request, summarize


def _ok(cid: str, latency_ms: int = 155, tokens_out: int = 120, cost: float = 0.0019) -> list[dict]:
    return [
        {"event": "request_received", "correlation_id": cid},
        {
            "event": "response_sent",
            "correlation_id": cid,
            "latency_ms": latency_ms,
            "ttft_ms": 50,
            "tokens_in": 30,
            "tokens_out": tokens_out,
            "cost_usd": cost,
            "quality_score": 0.9,
            "tool_success": True,
        },
    ]


BASE = summarize(_ok("req-a") + _ok("req-b"))


def test_detects_retrieval_failure_and_picks_failed_request() -> None:
    rows = [
        {"event": "request_received", "correlation_id": "req-f"},
        {"event": "request_failed", "correlation_id": "req-f", "error_type": "RuntimeError", "tool_success": False},
    ]
    findings = detect(BASE, summarize(rows))
    assert findings[0][0] == "error_rate_pct"
    assert pick_request(rows, "error_rate_pct")["correlation_id"] == "req-f"


def test_detects_latency_regression_and_picks_slowest_request() -> None:
    rows = _ok("req-s1", latency_ms=2650) + _ok("req-s2", latency_ms=2700)
    findings = detect(BASE, summarize(rows))
    assert [f[0] for f in findings] == ["latency_ms"]
    assert pick_request(rows, "latency_ms")["correlation_id"] == "req-s2"


def test_detects_cost_spike_and_picks_most_expensive_request() -> None:
    rows = _ok("req-c1", tokens_out=500, cost=0.0076) + _ok("req-c2", tokens_out=720, cost=0.0109)
    findings = detect(BASE, summarize(rows))
    assert [f[0] for f in findings] == ["cost_usd"]
    assert pick_request(rows, "cost_usd")["correlation_id"] == "req-c2"


def test_normal_window_has_no_findings() -> None:
    assert detect(BASE, summarize(_ok("req-n"))) == []
