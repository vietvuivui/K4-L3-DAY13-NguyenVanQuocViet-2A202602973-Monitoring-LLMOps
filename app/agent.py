from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import metrics
from .mock_llm import FakeLLM, FakeResponse
from .mock_rag import retrieve
from .pii import hash_user_id, summarize_text
from .prompt_management import resolve_prompt
from .tracing import get_langfuse_client, observe, propagate_attributes, tracing_enabled


@dataclass
class AgentResult:
    answer: str
    latency_ms: int
    ttft_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    quality_score: float


class LabAgent:
    def __init__(self, model: str = "claude-sonnet-4-5") -> None:
        self.model = model
        self.llm = FakeLLM(model=model)

    @observe(name="lab-agent-run", as_type="agent", capture_input=False, capture_output=False)
    def run(
        self,
        user_id: str,
        feature: str,
        session_id: str,
        message: str,
        correlation_id: str,
    ) -> AgentResult:
        langfuse_client = get_langfuse_client()
        with propagate_attributes(
            user_id=hash_user_id(user_id),
            session_id=session_id,
            tags=["lab", feature, self.model],
            trace_name="day13-agent-request",
            environment=os.getenv("APP_ENV", "dev"),
            metadata={
                "feature": feature,
                "model": self.model,
                "correlation_id": correlation_id,
            },
        ):
            started = time.perf_counter()
            query_preview = summarize_text(message)
            docs = self._traced_retrieve(langfuse_client, message, query_preview)
            # Span riêng để waterfall thấy thời gian fetch prompt từ Langfuse.
            with langfuse_client.start_as_current_observation(
                name="prompt-resolve", as_type="span"
            ) as prompt_span:
                prompt = resolve_prompt(
                    langfuse_client,
                    feature=feature,
                    docs=docs,
                    message=message,
                    enabled=tracing_enabled(),
                )
                prompt_span.update(
                    metadata={
                        "prompt_name": prompt.name,
                        "prompt_label": prompt.label,
                        "prompt_version": prompt.version,
                        "prompt_source": prompt.source,
                    },
                    level="WARNING" if prompt.fetch_error else None,
                    status_message=prompt.fetch_error,
                )
            langfuse_client.update_current_span(
                metadata={
                    "doc_count": len(docs),
                    "query_preview": query_preview,
                    "prompt_name": prompt.name,
                    "prompt_label": prompt.label,
                    "prompt_version": prompt.version,
                    "prompt_source": prompt.source,
                    "prompt_fetch_error": prompt.fetch_error or "",
                },
                version=prompt.version,
            )
            with propagate_attributes(prompt=prompt.managed_prompt):
                response, cost_usd = self._traced_generate(langfuse_client, prompt, query_preview)
            quality_score = self._heuristic_quality(message, response.text, docs)
            latency_ms = int((time.perf_counter() - started) * 1000)

        metrics.record_request(
            latency_ms=latency_ms,
            ttft_ms=response.ttft_ms,
            cost_usd=cost_usd,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            quality_score=quality_score,
        )

        return AgentResult(
            answer=response.text,
            latency_ms=latency_ms,
            ttft_ms=response.ttft_ms,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            cost_usd=cost_usd,
            quality_score=quality_score,
        )

    def _traced_retrieve(self, langfuse_client, message: str, query_preview: str) -> list[str]:
        # Chỉ gửi query đã scrub lên Langfuse; message gốc có thể chứa PII.
        with langfuse_client.start_as_current_observation(
            name="retrieval",
            as_type="retriever",
            input={"query_preview": query_preview},
        ) as observation:
            try:
                docs = retrieve(message)
            except Exception as exc:
                observation.update(
                    level="ERROR",
                    status_message=type(exc).__name__,
                    metadata={"tool_success": False, "error_type": type(exc).__name__},
                )
                raise
            observation.update(
                output={"doc_count": len(docs), "doc_previews": [summarize_text(d, 60) for d in docs]},
                metadata={"tool_success": True, "doc_count": len(docs)},
            )
            return docs

    def _traced_generate(self, langfuse_client, prompt, query_preview: str) -> tuple[FakeResponse, float]:
        # Không capture prompt đã compile (chứa message gốc); chỉ ghi preview đã scrub.
        with langfuse_client.start_as_current_observation(
            name="llm-generate",
            as_type="generation",
            model=self.model,
            prompt=prompt.managed_prompt,
            input={
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
                "query_preview": query_preview,
            },
            metadata={"prompt_label": prompt.label, "prompt_source": prompt.source},
        ) as generation:
            started_at = datetime.now(timezone.utc)
            response = self.llm.generate(prompt.text)
            cost_usd = self._estimate_cost(response.usage.input_tokens, response.usage.output_tokens)
            generation.update(
                output={"answer_preview": summarize_text(response.text)},
                completion_start_time=started_at + timedelta(milliseconds=response.ttft_ms),
                usage_details={
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                    "total": response.usage.input_tokens + response.usage.output_tokens,
                },
                cost_details={
                    "input": round(response.usage.input_tokens / 1_000_000 * 3, 6),
                    "output": round(response.usage.output_tokens / 1_000_000 * 15, 6),
                    "total": cost_usd,
                },
                metadata={"ttft_ms": response.ttft_ms},
            )
            return response, cost_usd

    def _estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        input_cost = (tokens_in / 1_000_000) * 3
        output_cost = (tokens_out / 1_000_000) * 15
        return round(input_cost + output_cost, 6)

    def _heuristic_quality(self, question: str, answer: str, docs: list[str]) -> float:
        score = 0.5
        if docs:
            score += 0.2
        if len(answer) > 40:
            score += 0.1
        if question.lower().split()[0:1] and any(token in answer.lower() for token in question.lower().split()[:3]):
            score += 0.1
        if "[REDACTED" in answer:
            score -= 0.2
        return round(max(0.0, min(1.0, score)), 2)
