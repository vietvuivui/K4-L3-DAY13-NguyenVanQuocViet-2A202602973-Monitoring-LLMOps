from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from app import logging_config
from app.main import app

BODY = {"user_id": "student-01", "session_id": "session-01", "feature": "qa", "message": "Explain traces"}


def _post(headers: dict[str, str] | None = None, body: dict | None = None) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/chat", json=body or BODY, headers=headers or {})

    return asyncio.run(send())


def test_reuses_incoming_request_id_in_header_body_and_logs(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)

    response = _post({"x-request-id": "req-cafe1234"})

    assert response.headers["x-request-id"] == "req-cafe1234"
    assert float(response.headers["x-response-time-ms"]) >= 0
    assert response.json()["correlation_id"] == "req-cafe1234"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert {e["correlation_id"] for e in events} == {"req-cafe1234"}


def test_generates_id_when_missing_or_unsafe() -> None:
    for headers in ({}, {"x-request-id": "bad id<script>"}):
        response = _post(headers)
        assert re.fullmatch(r"req-[0-9a-f]{8}", response.headers["x-request-id"])


def test_context_does_not_leak_between_requests(monkeypatch, tmp_path: Path) -> None:
    log_path = tmp_path / "logs.jsonl"
    monkeypatch.setattr(logging_config, "LOG_PATH", log_path)

    _post({"x-request-id": "req-aaaa0001"}, {**BODY, "session_id": "s-first", "feature": "qa"})
    _post({"x-request-id": "req-bbbb0002"}, {**BODY, "session_id": "s-second", "feature": "summary"})

    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    second = [e for e in events if e["correlation_id"] == "req-bbbb0002"]
    assert second and all(e["session_id"] == "s-second" and e["feature"] == "summary" for e in second)
