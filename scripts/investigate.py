"""Điều tra sự cố theo thứ tự Metrics -> Logs -> Traces.

Ví dụ:
    python scripts/investigate.py --start 2026-09-29T09:00:00Z --end 2026-09-29T09:05:00Z
    python scripts/investigate.py --last-minutes 5          # cửa sổ sự cố = 5 phút gần nhất

Cửa sổ baseline mặc định là khoảng thời gian cùng độ dài ngay trước cửa sổ sự cố.
Bước Traces gọi Langfuse Observations API v2 bằng key trong .env (không in key ra).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio
from scripts.dashboard import load_records, percentile

LOG_PATH = REPO_ROOT / "data" / "logs.jsonl"


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def summarize(rows: list[dict]) -> dict:
    received = [r for r in rows if r.get("event") == "request_received"]
    sent = [r for r in rows if r.get("event") == "response_sent"]
    failed = [r for r in rows if r.get("event") == "request_failed"]
    tools = [r for r in rows if r.get("tool_success") is not None]
    latency = [r["latency_ms"] for r in sent if "latency_ms" in r]

    def avg(values):
        return round(sum(values) / len(values), 6) if values else None

    return {
        "requests": len(received),
        "latency_p50_ms": percentile(latency, 50),
        "latency_p95_ms": percentile(latency, 95),
        "ttft_p95_ms": percentile([r["ttft_ms"] for r in sent if "ttft_ms" in r], 95),
        "error_rate_pct": round(len(failed) / len(received) * 100, 2) if received else None,
        "error_types": dict(Counter(r.get("error_type", "unknown") for r in failed)),
        "retrieval_success_pct": (
            round(sum(r["tool_success"] is True for r in tools) / len(tools) * 100, 2) if tools else None
        ),
        "avg_cost_usd": avg([r.get("cost_usd", 0.0) for r in sent]),
        "avg_tokens_in": avg([r.get("tokens_in", 0) for r in sent]),
        "avg_tokens_out": avg([r.get("tokens_out", 0) for r in sent]),
        "quality_mean": avg([r["quality_score"] for r in sent if "quality_score" in r]),
    }


def detect(base: dict, incident: dict) -> list[tuple[str, str]]:
    """Trả về danh sách (metric, mô tả) bất thường, xếp theo mức nghiêm trọng."""
    findings = []
    if (incident["error_rate_pct"] or 0) > 2:
        findings.append(("error_rate_pct", f"error rate {base['error_rate_pct']}% -> {incident['error_rate_pct']}% "
                         f"({incident['error_types']}); retrieval success {base['retrieval_success_pct']}% -> "
                         f"{incident['retrieval_success_pct']}%"))
    if base["latency_p50_ms"] and incident["latency_p50_ms"] and incident["latency_p95_ms"] > 2000 \
            and incident["latency_p50_ms"] > 2 * base["latency_p50_ms"]:
        findings.append(("latency_ms", f"latency P50 {base['latency_p50_ms']} -> {incident['latency_p50_ms']} ms, "
                         f"P95 {base['latency_p95_ms']} -> {incident['latency_p95_ms']} ms"))
    if base["avg_cost_usd"] and incident["avg_cost_usd"] and incident["avg_cost_usd"] > 2 * base["avg_cost_usd"]:
        findings.append(("cost_usd", f"avg cost/request {base['avg_cost_usd']} -> {incident['avg_cost_usd']} USD, "
                         f"avg tokens_out {base['avg_tokens_out']} -> {incident['avg_tokens_out']}"))
    return findings


def pick_request(rows: list[dict], metric: str) -> dict | None:
    if metric == "error_rate_pct":
        return next((r for r in rows if r.get("event") == "request_failed"), None)
    sent = [r for r in rows if r.get("event") == "response_sent" and metric in r]
    return max(sent, key=lambda r: r[metric], default=None)


def fetch_trace(correlation_id: str, start: datetime, end: datetime) -> list[dict]:
    load_dotenv(REPO_ROOT / ".env")
    base_url = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    auth = (os.getenv("LANGFUSE_PUBLIC_KEY", ""), os.getenv("LANGFUSE_SECRET_KEY", ""))
    if not all(auth):
        print("  (thiếu Langfuse key trong .env — bỏ qua bước trace)")
        return []
    response = httpx.get(
        f"{base_url}/api/public/v2/observations",
        auth=auth,
        params={
            "fromStartTime": (start - timedelta(minutes=1)).isoformat(),
            "toStartTime": (end + timedelta(minutes=1)).isoformat(),
            "limit": 1000,
            "fields": "core,basic,metadata,usage,model,prompt",
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        o for o in response.json()["data"]
        if (o.get("metadata") or {}).get("correlation_id") == correlation_id
    ]


def print_trace(observations: list[dict]) -> None:
    if not observations:
        print("  Không tìm thấy trace (SDK có thể chưa flush — thử lại sau vài giây).")
        return
    trace_id = observations[0]["traceId"]
    print(f"  trace_id: {trace_id}")
    for o in sorted(observations, key=lambda o: (o.get("parentObservationId") is not None, o["startTime"])):
        duration = (_parse(o["endTime"]) - _parse(o["startTime"])).total_seconds() * 1000 if o.get("endTime") else None
        extra = ""
        if o["type"] == "GENERATION":
            extra = (f" model={o.get('model')} usage={o.get('usageDetails')} "
                     f"cost={o.get('costDetails', {}).get('total')} prompt={o.get('promptName')} v{o.get('promptVersion')}")
        indent = "    " if o.get("parentObservationId") else "  "
        print(f"{indent}{o['name']} [{o['type']}] {duration:.0f} ms level={o['level']} "
              f"{o.get('statusMessage') or ''}{extra}")


def main() -> None:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="ISO time, UTC")
    parser.add_argument("--end", help="ISO time, UTC")
    parser.add_argument("--last-minutes", type=float, default=5)
    parser.add_argument("--baseline-minutes", type=float, help="Mặc định bằng độ dài cửa sổ sự cố")
    args = parser.parse_args()

    end = _parse(args.end) if args.end else datetime.now(timezone.utc)
    start = _parse(args.start) if args.start else end - timedelta(minutes=args.last_minutes)
    base_len = timedelta(minutes=args.baseline_minutes) if args.baseline_minutes else end - start

    records = load_records(LOG_PATH)
    incident_rows = [r for r in records if start <= r["_ts"] <= end]
    base_rows = [r for r in records if start - base_len <= r["_ts"] < start]
    base, incident = summarize(base_rows), summarize(incident_rows)

    print(f"== 1. METRICS  (incident {start:%H:%M:%S}–{end:%H:%M:%S} UTC vs baseline {base_len} trước đó)")
    for key in incident:
        print(f"  {key:22} baseline={base[key]!s:28} incident={incident[key]}")
    findings = detect(base, incident)
    if not findings:
        print("  Không phát hiện metric bất thường rõ ràng trong cửa sổ này.")
        return
    for metric, text in findings:
        print(f"  ! {metric}: {text}")

    metric = findings[0][0]
    chosen = pick_request(incident_rows, metric)
    if chosen is None:
        return
    cid = chosen["correlation_id"]
    print(f"\n== 2. LOGS  (request đại diện cho '{metric}': correlation_id={cid})")
    for r in records:
        if r.get("correlation_id") == cid:
            print("  " + json.dumps({k: v for k, v in r.items() if k != "_ts"}, ensure_ascii=False))

    print(f"\n== 3. TRACES  (metadata.correlation_id == {cid})")
    print_trace(fetch_trace(cid, start, end))


if __name__ == "__main__":
    main()
