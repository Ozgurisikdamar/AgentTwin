from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import agenttwin
from agenttwin import AgentTwin, Config, tool_span
from agenttwin.hashing import canonical_json, sha256_hex


def by_name(spans: tuple[ReadableSpan, ...]) -> dict[str, ReadableSpan]:
    return {s.name: s for s in spans}


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


CONTENT_KEYS = {
    "agenttwin.input",
    "agenttwin.output",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
}


def run_refund(client: AgentTwin, **run_kwargs: Any) -> str:
    with client.agent_run(
        "support-refund-agent",
        "1.3.0",
        input="Refund ORD-1001, my email is jane@example.com. password=hunter2hunter",
        session_id="sess-1",
        **run_kwargs,
    ) as run:
        with run.model_call(
            "anthropic",
            "claude-sonnet-5",
            input_messages=[{"role": "user", "content": "contact jane@example.com"}],
            system_instructions="Always call get_refund_policy before refund_payment.",
            temperature=0.0,
            prompt_hash="abc",
        ) as call:
            call.record_response(
                output_messages=[{"role": "assistant", "content": "ok"}],
                input_tokens=120,
                output_tokens=12,
                finish_reasons=["tool_use"],
                response_model="claude-sonnet-5",
            )
        with run.retrieval("support-kb", query="refund policy") as ret:
            ret.set_documents([{"id": "kb-1"}, {"id": "kb-2"}])
        with run.tool_call(
            "refund_payment",
            args={
                "order_id": "ORD-1001",
                "amount": 50.0,
                "note": "api_key: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
            },
            risk="WRITE_IRREVERSIBLE",
            idempotency_key="refund-ORD-1001",
            attempt=1,
        ) as tool:
            tool.set_result({"refund_id": "R-1", "status": "succeeded"})
        run.policy_decision(
            "allow", policy="refund-limits", version="3", rule="amount<=100", tool="refund_payment"
        )
        run.outcome(
            "SUCCESS",
            business_outcome="REFUND_COMPLETED",
            claimed="SUCCESS",
            verified=True,
            verification_source="tool_result",
            state_diff={"refunds": [0, 1]},
        )
        run.set_output("Your refund of 50 USD was issued. Mail sent to jane@example.com")
        return run.trace_id


def test_span_contract_and_hierarchy(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client(content_mode="off", environment="production", release_id="rel-1")
    trace_id = run_refund(
        client, source="simulation", simulation_run_id="run-9", scenario_id="refund-happy-path"
    )
    assert client.flush()
    spans = by_name(exporter.get_finished_spans())
    root = spans["invoke_agent support-refund-agent"]
    assert format(root.context.trace_id, "032x") == trace_id
    assert root.parent is None
    for name in (
        "chat claude-sonnet-5",
        "retrieval support-kb",
        "execute_tool refund_payment",
        "policy.decision",
        "outcome.verify",
    ):
        assert spans[name].parent is not None and spans[name].parent.span_id == root.context.span_id, name

    ra = attrs(root)
    assert ra["agenttwin.span.kind"] == "agent"
    assert ra["gen_ai.operation.name"] == "invoke_agent"
    assert ra["gen_ai.conversation.id"] == "sess-1"

    # Run context is copied onto every span of the run.
    for span in spans.values():
        a = attrs(span)
        assert a["agenttwin.agent.name"] == "support-refund-agent"
        assert a["agenttwin.agent.version"] == "1.3.0"
        assert a["agenttwin.source"] == "simulation"
        assert a["agenttwin.environment"] == "production"
        assert a["agenttwin.simulation.run_id"] == "run-9"
        assert a["agenttwin.scenario.id"] == "refund-happy-path"
        assert a["agenttwin.release.id"] == "rel-1"
        # Content capture is off by default: nothing that could carry content.
        assert CONTENT_KEYS.isdisjoint(a), f"content leaked on {span.name}: {CONTENT_KEYS & set(a)}"

    model = attrs(spans["chat claude-sonnet-5"])
    assert model["gen_ai.provider.name"] == "anthropic"
    assert model["gen_ai.request.model"] == "claude-sonnet-5"
    assert model["gen_ai.usage.input_tokens"] == 120
    assert model["gen_ai.usage.output_tokens"] == 12
    assert list(model["gen_ai.response.finish_reasons"]) == ["tool_use"]
    assert model["agenttwin.prompt.hash"] == "abc"

    tool = attrs(spans["execute_tool refund_payment"])
    args = {"order_id": "ORD-1001", "amount": 50.0, "note": "api_key: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"}
    assert tool["gen_ai.tool.name"] == "refund_payment"
    assert tool["agenttwin.tool.risk"] == "WRITE_IRREVERSIBLE"
    assert tool["agenttwin.tool.args_hash"] == sha256_hex(canonical_json(args))
    assert tool["agenttwin.tool.idempotency_key_hash"] == sha256_hex("refund-ORD-1001")[:16]
    assert "refund-ORD-1001" not in json.dumps(tool), "raw idempotency key must never be exported"
    assert tool["agenttwin.tool.result_status"] == "ok"
    assert tool["agenttwin.tool.attempt"] == 1

    assert attrs(spans["retrieval support-kb"])["agenttwin.retrieval.document_count"] == 2
    policy = attrs(spans["policy.decision"])
    assert (policy["agenttwin.policy.decision"], policy["agenttwin.policy.rule"]) == ("allow", "amount<=100")
    out = attrs(spans["outcome.verify"])
    assert out["agenttwin.outcome.status"] == "SUCCESS"
    assert out["agenttwin.outcome.verified"] is True
    assert out["agenttwin.outcome.verification_source"] == "tool_result"
    assert json.loads(out["agenttwin.state.diff"]) == {"refunds": [0, 1]}

    resource = dict(root.resource.attributes)
    assert resource["agenttwin.sdk.name"] == "agenttwin-python"
    assert resource["agenttwin.content.mode"] == "off"
    assert resource["agenttwin.content.redacted"] is False
    assert resource["deployment.environment.name"] == "production"


def test_redacted_mode_masks_pii_and_secrets(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client(content_mode="redacted")
    run_refund(client)
    client.flush()
    spans = by_name(exporter.get_finished_spans())
    root = attrs(spans["invoke_agent support-refund-agent"])
    assert root["agenttwin.input"] == (
        "Refund ORD-1001, my email is [REDACTED:email]. password=[REDACTED:credential]"
    )
    assert "jane@example.com" not in json.dumps(
        {k: str(v) for s in spans.values() for k, v in attrs(s).items()}
    )
    tool = attrs(spans["execute_tool refund_payment"])
    assert json.loads(tool["gen_ai.tool.call.arguments"]) == {
        "order_id": "ORD-1001",
        "amount": 50.0,
        "note": "api_key: [REDACTED:api_key]",
    }
    assert json.loads(tool["gen_ai.tool.call.result"]) == {"refund_id": "R-1", "status": "succeeded"}
    model = attrs(spans["chat claude-sonnet-5"])
    assert json.loads(model["gen_ai.input.messages"]) == [
        {"role": "user", "content": "contact [REDACTED:email]"}
    ]
    assert dict(spans["outcome.verify"].resource.attributes)["agenttwin.content.redacted"] is True


def test_full_mode_keeps_pii_but_never_secrets(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client(content_mode="full")
    run_refund(client)
    client.flush()
    root = attrs(by_name(exporter.get_finished_spans())["invoke_agent support-refund-agent"])
    assert (
        root["agenttwin.input"]
        == "Refund ORD-1001, my email is jane@example.com. password=[REDACTED:credential]"
    )


def test_json_path_redaction_applies_to_tool_arguments(
    make_client: Any, exporter: InMemorySpanExporter
) -> None:
    client = make_client(
        content_mode="redacted",
        redaction=agenttwin.RedactionConfig(json_paths=("$.customer.name",), strategy="hash"),
    )
    with (
        client.agent_run("a", "1") as run,
        run.tool_call(
            "lookup_customer", args={"customer": {"name": "Jane Roe", "tier": "gold"}}, risk="READ"
        ) as t,
    ):
        t.set_result({"ok": True})
    client.flush()
    tool = attrs(by_name(exporter.get_finished_spans())["execute_tool lookup_customer"])
    arguments = json.loads(tool["gen_ai.tool.call.arguments"])
    assert arguments["customer"]["tier"] == "gold"
    assert arguments["customer"]["name"].startswith("[HASH:field:")


def test_run_context_reaches_spans_started_in_other_threads(
    make_client: Any, exporter: InMemorySpanExporter
) -> None:
    client = make_client()
    with client.agent_run("threaded-agent", "2.0", source="replay") as run:
        parent_ctx = run.span.get_span_context()

        def work() -> None:
            from opentelemetry import trace as otel_trace

            ctx = otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(parent_ctx))
            span = client.tracer.start_span("worker step", context=ctx)
            span.end()

        t = threading.Thread(target=work)
        t.start()
        t.join()
    client.flush()
    worker = attrs(by_name(exporter.get_finished_spans())["worker step"])
    assert worker["agenttwin.agent.name"] == "threaded-agent"
    assert worker["agenttwin.source"] == "replay"


def test_two_concurrent_runs_keep_their_own_context(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client()

    async def one(version: str) -> None:
        with client.agent_run("multi", version) as run:
            await asyncio.sleep(0.01)
            with run.tool_call("lookup_order", args={"v": version}, risk="READ") as t:
                await asyncio.sleep(0.01)
                t.set_result({})

    async def main() -> None:
        await asyncio.gather(one("1.0"), one("2.0"))

    asyncio.run(main())
    client.flush()
    tools = [s for s in exporter.get_finished_spans() if s.name == "execute_tool lookup_order"]
    assert len(tools) == 2
    for s in tools:
        a = attrs(s)
        roots = [r for r in exporter.get_finished_spans() if r.context.span_id == s.parent.span_id]  # type: ignore[union-attr]
        assert attrs(roots[0])["agenttwin.agent.version"] == a["agenttwin.agent.version"]
    assert {attrs(s)["agenttwin.agent.version"] for s in tools} == {"1.0", "2.0"}


def test_tool_errors_and_timeouts(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client()
    with client.agent_run("a", "1") as run:
        with pytest.raises(TimeoutError), run.tool_call("refund_payment", args={}, risk="WRITE_IRREVERSIBLE"):
            raise TimeoutError("read timed out")
        with run.tool_call("refund_payment", args={}, risk="WRITE_IRREVERSIBLE", attempt=2) as t:
            t.set_error("rate_limited", http_status=429)
        with pytest.raises(ValueError), run.tool_call("x", args={}, risk="NOT_A_RISK"):
            pass
    client.flush()
    tools = [s for s in exporter.get_finished_spans() if s.name == "execute_tool refund_payment"]
    first, second = sorted(tools, key=lambda s: s.start_time or 0)
    assert attrs(first)["agenttwin.tool.result_status"] == "timeout"
    assert attrs(first)["error.type"] == "TimeoutError"
    assert first.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in first.events)
    assert attrs(second)["agenttwin.tool.result_status"] == "rate_limited"
    assert attrs(second)["http.response.status_code"] == 429
    assert second.status.status_code == StatusCode.ERROR


def test_outcome_validation() -> None:
    client = AgentTwin(Config(enabled=False))
    with client.agent_run("a") as run:
        with pytest.raises(ValueError, match="cannot be verified"):
            run.outcome("SUCCESS", verified=True, verification_source="unavailable")
        with pytest.raises(ValueError):
            run.outcome("DONE")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            run.outcome("SUCCESS", verification_source="self_report")  # type: ignore[arg-type]


def test_tool_span_decorator_sync_and_async(exporter: InMemorySpanExporter) -> None:
    agenttwin.configure(Config(content_mode="redacted", schedule_delay_ms=50))
    # Swap the default client's exporter for the in-memory one.
    client = AgentTwin(Config(content_mode="redacted", schedule_delay_ms=50), exporter=exporter)
    import agenttwin.tracing as tracing_mod

    tracing_mod._default_client = client

    @tool_span(name="refund_payment", risk="WRITE_IRREVERSIBLE")
    def refund_payment(order_id: str, amount: float, idempotency_key: str | None = None) -> dict[str, Any]:
        return {"refund_id": f"R-{order_id}", "amount": amount}

    @tool_span(risk="READ")
    async def lookup_order(order_id: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"order_id": order_id, "email": "jane@example.com"}

    @tool_span(risk="READ")
    def broken(order_id: str) -> None:
        raise RuntimeError("upstream exploded")

    with agenttwin.AgentTrace(agent="refund-agent", version="1.3.0", project="support") as trace:
        assert agenttwin.current_run() is trace
        refund_payment("ORD-1", 50.0, idempotency_key="k-1")
        asyncio.run(lookup_order("ORD-1"))
        with pytest.raises(RuntimeError):
            broken("ORD-2")
        agenttwin.outcome("SUCCESS", business_outcome="REFUND_COMPLETED")
    assert agenttwin.current_run() is None
    client.flush()
    spans = by_name(exporter.get_finished_spans())
    refund = attrs(spans["execute_tool refund_payment"])
    assert json.loads(refund["gen_ai.tool.call.arguments"]) == {
        "order_id": "ORD-1",
        "amount": 50.0,
        "idempotency_key": "k-1",
    }
    assert refund["agenttwin.tool.idempotency_key_hash"] == sha256_hex("k-1")[:16]
    lookup = attrs(spans["execute_tool lookup_order"])
    assert json.loads(lookup["gen_ai.tool.call.result"])["email"] == "[REDACTED:email]"
    assert attrs(spans["execute_tool broken"])["agenttwin.tool.result_status"] == "error"
    assert spans["execute_tool broken"].status.status_code == StatusCode.ERROR
    root = attrs(spans["invoke_agent refund-agent"])
    assert root["agenttwin.project"] == "support"
    assert attrs(spans["outcome.verify"])["agenttwin.outcome.status"] == "SUCCESS"
    client.shutdown()
    tracing_mod._default_client = None


def test_sampling_ratio_zero_exports_nothing(make_client: Any, exporter: InMemorySpanExporter) -> None:
    client = make_client(sample_ratio=0.0)
    run_refund(client)
    client.flush()
    assert exporter.get_finished_spans() == ()


def test_config_from_env() -> None:
    cfg = Config.from_env(
        {
            "AGENTTWIN_API_KEY": "atk_x",
            "AGENTTWIN_OTLP_ENDPOINT": "http://collector:4318",
            "AGENTTWIN_CONTENT_MODE": "redacted",
            "AGENTTWIN_SAMPLE_RATIO": "0.25",
            "AGENTTWIN_SOURCE": "simulation",
            "AGENTTWIN_REDACTION_STRATEGY": "hash",
            "AGENTTWIN_REDACTION_JSON_PATHS": "$.a.b, $.c",
        },
        environment="staging",
    )
    assert (cfg.api_key, cfg.otlp_endpoint, cfg.content_mode, cfg.sample_ratio) == (
        "atk_x",
        "http://collector:4318",
        "redacted",
        0.25,
    )
    assert cfg.source == "simulation"
    assert cfg.environment == "staging"
    assert cfg.redaction.strategy == "hash"
    assert cfg.redaction.json_paths == ("$.a.b", "$.c")
    assert Config.from_env({}).content_mode == "off", "content capture must be off by default"
    with pytest.raises(ValueError):
        Config.from_env({"AGENTTWIN_CONTENT_MODE": "everything"})
