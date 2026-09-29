# Template Alert và Runbook

Mỗi alert phải dựa trên triệu chứng người dùng hoặc SLO, không dựa trực tiếp vào tên implementation nội bộ.

Quy trình chung cho mọi alert: **Metrics → Logs → Traces**. Mở dashboard (`python scripts/dashboard.py`, http://127.0.0.1:8050) để xác định panel và khoảng thời gian → lọc `data/logs.jsonl` lấy `correlation_id` của request bất thường → tìm trace trên Langfuse có metadata `correlation_id` giống vậy → so sánh các span `retrieval`, `prompt-resolve`, `llm-generate`.

## Alert 1

- Tên: `high_latency_p95`
- Severity: P2
- Duration: 5m
- Kênh thông báo: Slack `#day13-llm-alerts`
- SLI/SLO liên quan: `fast_successful_requests` (99.5% request trả lời thành công trong ≤ 2000 ms, cửa sổ 28 ngày)
- Điều kiện và thời gian duy trì: P95 của `latency_ms` (event `response_sent`) trong 5 phút > 2000 ms, kéo dài liên tục 5 phút
- Ảnh hưởng tới người dùng: câu trả lời chậm; khi có nhiều request đồng thời, người dùng có thể chờ lâu hơn nhiều so với `latency_ms` (đã quan sát client chờ ~13 s trong khi log ghi ~2.6 s)
- Ba bước kiểm tra đầu tiên:
  1. Dashboard panel **Latency**: P50 cũng tăng (toàn bộ request chậm → dependency chậm) hay chỉ P99 (một vài request)? TTFT P95 có đổi không (TTFT tăng → LLM chậm; không đổi → bước trước LLM chậm)?
  2. Lọc log: `event == "response_sent" and latency_ms > 2000`, lấy vài `correlation_id`; kiểm tra có dồn vào một `feature`/`model` nào không.
  3. Mở trace có cùng `correlation_id` trên Langfuse, so thời lượng `retrieval` / `prompt-resolve` / `llm-generate` để biết span nào chiếm thời gian; `prompt-resolve` có level WARNING nghĩa là fetch prompt lỗi và phải dùng local fallback.
- Mitigation tạm thời: nếu `retrieval` chậm → giảm top-k / bật cache retrieval / tắt incident `rag_slow` khi đang practice; nếu `prompt-resolve` chậm → kiểm tra Langfuse và tăng `cache_ttl_seconds`; nếu `llm-generate` chậm → chuyển sang model nhỏ hơn hoặc giới hạn `max_tokens`. Thông báo tình trạng trong kênh Slack.
- Owner: llm-platform-oncall (Nguyễn Văn Quốc Việt)

## Alert 2

- Tên: `high_error_rate_or_retrieval_failure`
- Severity: P1
- Duration: 3m
- Kênh thông báo: Slack `#day13-llm-alerts` (P1: gọi on-call ngay)
- SLI/SLO liên quan: `fast_successful_requests` (request lỗi bị tính là xấu và tiêu error budget); guardrails `error_rate_pct_max = 2`, `retrieval_success_rate_pct_min = 90`
- Điều kiện và thời gian duy trì: trong cửa sổ 5 phút, error rate (`request_failed` / `request_received`) > 2% **hoặc** retrieval success (`tool_success == true` / tổng bản ghi có `tool_success`) < 90%, kéo dài 3 phút
- Ảnh hưởng tới người dùng: người dùng nhận HTTP 500 hoặc câu trả lời không có tài liệu hỗ trợ
- Ba bước kiểm tra đầu tiên:
  1. Dashboard panel **Errors**: xem breakdown `error_type` và đường retrieval success; lỗi bắt đầu từ phút nào.
  2. Lọc log `event == "request_failed"`: đọc `error_type`, `tool_name`, `payload.detail` (ví dụ `RuntimeError` / `Vector store timeout` từ `retrieval`), lấy `correlation_id`.
  3. Mở trace có cùng `correlation_id`: observation nào có level ERROR (ví dụ `retrieval [RETRIEVER] ERROR RuntimeError`) để khoanh vùng dependency lỗi.
- Mitigation tạm thời: nếu vector store lỗi → trả câu trả lời fallback “chưa tìm được tài liệu” thay vì 500, bật retry có backoff, hoặc tắt incident `tool_fail` khi đang practice; nếu lỗi sau khi deploy/đổi prompt → rollback prompt label `production` về version trước trên Langfuse hoặc rollback bản deploy.
- Owner: llm-platform-oncall (Nguyễn Văn Quốc Việt)

## Alert 3

- Tên: `cost_per_request_spike`
- Severity: P3
- Duration: 15m
- Kênh thông báo: Slack `#day13-llm-cost`
- SLI/SLO liên quan: guardrail `daily_cost_usd_max = 2.5` (tương đương ~0.104 USD/giờ); baseline chi phí ~0.0017 USD/request
- Điều kiện và thời gian duy trì: trong 15 phút, chi phí trung bình mỗi request > 0.0035 USD (gấp ~2 lần baseline) **hoặc** tổng chi phí trong 1 giờ > 0.104 USD, kéo dài 15 phút
- Ảnh hưởng tới người dùng: không gây lỗi ngay nhưng câu trả lời dài bất thường và có nguy cơ vượt ngân sách, dẫn tới bị giới hạn/tắt dịch vụ
- Ba bước kiểm tra đầu tiên:
  1. Dashboard panel **Cost** và **Tokens**: chi phí tăng do `tokens_out` (câu trả lời dài hơn) hay `tokens_in` (prompt/context dài hơn), hay do traffic tăng (panel **Traffic**)?
  2. Lọc log `event == "response_sent"` có `cost_usd` cao nhất, lấy `correlation_id`, kiểm tra `feature` và `model`.
  3. Mở trace tương ứng: generation `llm-generate` có `usage` / `cost` và prompt version nào; so với trace trước khi spike (prompt version mới có làm câu trả lời dài hơn không).
- Mitigation tạm thời: rollback label `production` về prompt version trước nếu spike đến sau khi đổi prompt; đặt `max_tokens` cho generation; chuyển feature ít quan trọng sang model rẻ hơn; tắt incident `cost_spike` khi đang practice.
- Owner: llm-platform-oncall (Nguyễn Văn Quốc Việt)
