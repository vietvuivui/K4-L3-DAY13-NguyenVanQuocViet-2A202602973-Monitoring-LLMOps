"""Dashboard 6 panel đọc trực tiếp data/logs.jsonl theo contract config/dashboard.yaml.

Chạy:  python scripts/dashboard.py            -> mở http://127.0.0.1:8050
       python scripts/dashboard.py --print    -> in số liệu tổng hợp ra terminal

Chỉ dùng thư viện chuẩn + PyYAML; biểu đồ vẽ bằng Chart.js (CDN) trên trình duyệt.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.cli import configure_utf8_stdio

CONFIG_PATH = REPO_ROOT / "config" / "dashboard.yaml"
LOG_PATH = REPO_ROOT / "data" / "logs.jsonl"


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile, cùng cách tính với báo cáo baseline."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return float(ordered[index])


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def load_records(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(record.get("ts", ""))
        if ts is not None:
            record["_ts"] = ts
            records.append(record)
    return records


def _ratio_pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def compute(records: list[dict], now: datetime, window_minutes: int = 60) -> dict:
    """Tổng hợp số liệu cho 6 panel trong cửa sổ [now - window, now]."""
    start = now - timedelta(minutes=window_minutes)
    in_window = [r for r in records if start <= r["_ts"] <= now]
    minutes = [
        (start + timedelta(minutes=i + 1)).replace(second=0, microsecond=0)
        for i in range(window_minutes)
    ]
    labels = [m.strftime("%H:%M") for m in minutes]

    buckets: dict[str, list[dict]] = defaultdict(list)
    for record in in_window:
        buckets[record["_ts"].strftime("%H:%M")].append(record)

    def per_minute(fn):
        return [fn(buckets.get(label, [])) for label in labels]

    def events(rows: list[dict], name: str) -> list[dict]:
        return [r for r in rows if r.get("event") == name]

    sent = events(in_window, "response_sent")
    received = events(in_window, "request_received")
    failed = events(in_window, "request_failed")
    tool_rows = [r for r in in_window if r.get("tool_success") is not None]

    latency = [r["latency_ms"] for r in sent if "latency_ms" in r]
    ttft = [r["ttft_ms"] for r in sent if "ttft_ms" in r]
    costs = [r.get("cost_usd", 0.0) for r in sent]
    quality = [r["quality_score"] for r in sent if "quality_score" in r]

    cumulative_cost, running = [], 0.0
    for value in per_minute(lambda rows: sum(r.get("cost_usd", 0.0) for r in events(rows, "response_sent"))):
        running += value
        cumulative_cost.append(round(running, 6))

    def minute_pct(rows, field):
        values = [r[field] for r in events(rows, "response_sent") if field in r]
        return values

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_start": start.isoformat(timespec="seconds"),
        "labels": labels,
        "latency": {
            "stats": {
                "p50": percentile(latency, 50),
                "p95": percentile(latency, 95),
                "p99": percentile(latency, 99),
                "ttft_p95": percentile(ttft, 95),
                "n": len(latency),
            },
            "series": {
                "p50": per_minute(lambda rows: percentile(minute_pct(rows, "latency_ms"), 50)),
                "p95": per_minute(lambda rows: percentile(minute_pct(rows, "latency_ms"), 95)),
                "p99": per_minute(lambda rows: percentile(minute_pct(rows, "latency_ms"), 99)),
                "ttft_p95": per_minute(lambda rows: percentile(minute_pct(rows, "ttft_ms"), 95)),
            },
        },
        "traffic": {
            "stats": {
                "count": len(received),
                "rate_per_minute": round(len(received) / window_minutes, 2),
            },
            "series": {"count": per_minute(lambda rows: len(events(rows, "request_received")))},
        },
        "errors": {
            "stats": {
                "error_rate_pct": _ratio_pct(len(failed), len(received)),
                "tool_success_rate_pct": _ratio_pct(
                    sum(1 for r in tool_rows if r["tool_success"] is True), len(tool_rows)
                ),
                "count_by_value": dict(Counter(r.get("error_type", "unknown") for r in failed)),
            },
            "series": {
                "error_rate_pct": per_minute(
                    lambda rows: _ratio_pct(len(events(rows, "request_failed")), len(events(rows, "request_received")))
                ),
                "tool_success_rate_pct": per_minute(
                    lambda rows: _ratio_pct(
                        sum(1 for r in rows if r.get("tool_success") is True),
                        sum(1 for r in rows if r.get("tool_success") is not None),
                    )
                ),
            },
        },
        "cost": {
            "stats": {"total": round(sum(costs), 6)},
            "series": {
                "sum_by_minute": per_minute(
                    lambda rows: round(sum(r.get("cost_usd", 0.0) for r in events(rows, "response_sent")), 6)
                ),
                "cumulative": cumulative_cost,
            },
        },
        "tokens": {
            "stats": {
                "tokens_in": sum(r.get("tokens_in", 0) for r in sent),
                "tokens_out": sum(r.get("tokens_out", 0) for r in sent),
            },
            "series": {
                "tokens_in": per_minute(lambda rows: sum(r.get("tokens_in", 0) for r in events(rows, "response_sent"))),
                "tokens_out": per_minute(lambda rows: sum(r.get("tokens_out", 0) for r in events(rows, "response_sent"))),
            },
        },
        "quality": {
            "stats": {"mean": round(sum(quality) / len(quality), 3) if quality else None},
            "series": {
                "mean": per_minute(
                    lambda rows: (
                        round(sum(v) / len(v), 3) if (v := minute_pct(rows, "quality_score")) else None
                    )
                ),
            },
        },
    }


def threshold_value(panel: dict, data: dict) -> float | None:
    """Lấy giá trị dùng để so với threshold của panel."""
    stats = data[panel["id"]]["stats"]
    aggregation = panel["threshold"]["aggregation"]
    if aggregation == "sum_by_field":
        return stats["tokens_in"] + stats["tokens_out"]
    if aggregation == "total":
        return stats["total"]
    return stats.get(aggregation)


def evaluate(panel: dict, data: dict) -> str:
    value = threshold_value(panel, data)
    if value is None:
        return "NO_DATA"
    limit = panel["threshold"]["value"]
    ok = value <= limit if panel["threshold"]["operator"] == "lte" else value >= limit
    return "OK" if ok else "BREACH"


def build_payload(config: dict, records: list[dict], now: datetime | None = None) -> dict:
    dashboard = config["dashboard"]
    now = now or datetime.now(timezone.utc)
    data = compute(records, now, dashboard["time_range_minutes"])
    panels = []
    for panel in dashboard["panels"]:
        panels.append(
            {
                "id": panel["id"],
                "title": panel["title"],
                "unit": panel["unit"],
                "threshold": panel["threshold"],
                "status": evaluate(panel, data),
                "current": threshold_value(panel, data),
            }
        )
    return {
        "title": dashboard["title"],
        "time_range_minutes": dashboard["time_range_minutes"],
        "refresh_seconds": dashboard["refresh_seconds"],
        "panels": panels,
        "data": data,
    }


PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Day 13 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#f5f6f8;color:#1c1f24}
header{padding:14px 20px;background:#fff;border-bottom:1px solid #dde1e6;display:flex;flex-wrap:wrap;gap:16px;align-items:baseline}
header h1{font-size:18px;margin:0}.meta{font-size:13px;color:#5b6470}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;padding:14px}
.card{background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:12px 14px}
.card h2{font-size:15px;margin:0 0 4px;display:flex;justify-content:space-between;gap:8px}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.OK{background:#e3f4e8;color:#1b6b34}.BREACH{background:#fde4e2;color:#a3261b}.NO_DATA{background:#eceef1;color:#5b6470}
.stats{font-size:13px;color:#39414b;margin:2px 0 8px;line-height:1.5}.stats b{color:#1c1f24}
canvas{max-height:220px}
</style></head><body>
<header><h1 id="title"></h1><span class="meta" id="meta"></span></header>
<main id="grid"></main>
<script>
const charts={};
const fmt=(v,d=1)=>v==null?"–":(typeof v==="number"?v.toLocaleString(undefined,{maximumFractionDigits:d}):v);
function thresholdLine(labels,value){return{label:"threshold",data:labels.map(()=>value),borderColor:"#d33",borderDash:[6,4],pointRadius:0,borderWidth:1.5,fill:false}}
function line(label,data,color){return{label,data,borderColor:color,backgroundColor:color,spanGaps:true,pointRadius:2,borderWidth:2,tension:.2}}
function bar(label,data,color){return{type:"bar",label,data,backgroundColor:color}}
function spec(p,d,L){
 const t=p.threshold.value,s=d[p.id].series,st=d[p.id].stats;
 switch(p.id){
 case"latency":return{stats:`P50 <b>${fmt(st.p50,0)}</b> · P95 <b>${fmt(st.p95,0)}</b> · P99 <b>${fmt(st.p99,0)}</b> · TTFT P95 <b>${fmt(st.ttft_p95,0)}</b> ms · n=${st.n}`,
  sets:[line("P50",s.p50,"#4a7bd0"),line("P95",s.p95,"#e08a1e"),line("P99",s.p99,"#8e44ad"),line("TTFT P95",s.ttft_p95,"#2a9d8f"),thresholdLine(L,t)]};
 case"traffic":return{stats:`Tổng <b>${st.count}</b> request · trung bình <b>${fmt(st.rate_per_minute,2)}</b> req/phút`,
  sets:[bar("requests/min",s.count,"#4a7bd0"),thresholdLine(L,t)]};
 case"errors":{const br=Object.entries(st.count_by_value).map(([k,v])=>`${k}: ${v}`).join(", ")||"không có lỗi";
  return{stats:`Error rate <b>${fmt(st.error_rate_pct,2)}%</b> · Retrieval success <b>${fmt(st.tool_success_rate_pct,2)}%</b> · Breakdown: ${br}`,
  sets:[line("error rate %",s.error_rate_pct,"#d33"),line("retrieval success %",s.tool_success_rate_pct,"#2a9d8f"),thresholdLine(L,t)]}}
 case"cost":return{stats:`Tổng <b>$${fmt(st.total,6)}</b> trong cửa sổ`,
  sets:[bar("USD/phút",s.sum_by_minute,"#e08a1e"),line("tích lũy USD",s.cumulative,"#8e44ad"),thresholdLine(L,t)]};
 case"tokens":return{stats:`Input <b>${fmt(st.tokens_in,0)}</b> · Output <b>${fmt(st.tokens_out,0)}</b> · Tổng <b>${fmt(st.tokens_in+st.tokens_out,0)}</b> tokens`,
  sets:[bar("tokens_in",s.tokens_in,"#4a7bd0"),bar("tokens_out",s.tokens_out,"#e08a1e")],stacked:true};
 case"quality":return{stats:`Mean <b>${fmt(st.mean,3)}</b>`,sets:[line("mean quality",s.mean,"#2a9d8f"),thresholdLine(L,t)],max:1};
 }}
async function refresh(){
 const r=await fetch("/api/data");const P=await r.json();const d=P.data,L=d.labels;
 document.getElementById("title").textContent=P.title;
 document.getElementById("meta").textContent=`Time range: ${P.time_range_minutes} phút gần nhất (${d.window_start} → ${d.generated_at} UTC) · refresh ${P.refresh_seconds}s · nguồn data/logs.jsonl`;
 const grid=document.getElementById("grid");
 for(const p of P.panels){
  let card=document.getElementById("card-"+p.id);
  if(!card){card=document.createElement("div");card.className="card";card.id="card-"+p.id;
   card.innerHTML=`<h2><span></span><span class="badge"></span></h2><div class="stats"></div><canvas></canvas>`;grid.appendChild(card)}
  const th=p.threshold,op=th.operator==="lte"?"≤":"≥";
  card.querySelector("h2 span").textContent=`${p.title} (${p.unit})`;
  const b=card.querySelector(".badge");b.className="badge "+p.status;b.textContent=`${p.status} · ${th.aggregation} ${op} ${th.value}`;
  const sp=spec(p,d,L);card.querySelector(".stats").innerHTML=sp.stats+` · threshold: ${th.aggregation} ${op} ${th.value} ${p.unit}`;
  const cfg={type:"line",data:{labels:L,datasets:sp.sets},options:{animation:false,responsive:true,interaction:{mode:"index",intersect:false},
   plugins:{legend:{labels:{boxWidth:12,font:{size:11}}}},scales:{x:{stacked:!!sp.stacked,ticks:{maxTicksLimit:12}},y:{stacked:!!sp.stacked,beginAtZero:true,max:sp.max,title:{display:true,text:p.unit}}}}};
  if(charts[p.id]){charts[p.id].data=cfg.data;charts[p.id].update()}else{charts[p.id]=new Chart(card.querySelector("canvas"),cfg)}
 }
 setTimeout(refresh,P.refresh_seconds*1000);
}
refresh();
</script></body></html>"""


def make_handler(config: dict, log_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/data":
                body = json.dumps(build_payload(config, load_records(log_path))).encode("utf-8")
                content_type = "application/json"
            elif self.path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    return Handler


def main() -> None:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--print", action="store_true", help="In số liệu tổng hợp rồi thoát")
    args = parser.parse_args()

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if args.print:
        payload = build_payload(config, load_records(LOG_PATH))
        print(f"{payload['title']} | last {payload['time_range_minutes']} min")
        for panel in payload["panels"]:
            th = panel["threshold"]
            print(f"- [{panel['status']}] {panel['title']} ({panel['unit']}): "
                  f"{th['aggregation']}={panel['current']} (threshold {th['operator']} {th['value']})")
            print(f"    {payload['data'][panel['id']]['stats']}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(config, LOG_PATH))
    print(f"Dashboard: http://127.0.0.1:{args.port}  (Ctrl+C để dừng)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
