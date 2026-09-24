# AgentTwin Python SDK

Instruments a tool-using agent so AgentTwin can reconstruct each run: the agent
run, model calls, tool calls, retrievals, policy decisions and the outcome.
Spans follow the OpenTelemetry GenAI semantic conventions plus `agenttwin.*`
attributes, and are exported over OTLP/HTTP to the AgentTwin collector.

* Python 3.10+ (tested on 3.12); depends only on the OpenTelemetry API, SDK
  and OTLP/HTTP exporter.
* **Content capture is off by default**: only metadata, hashes and counts
  leave the process (ADR-0008).
* Telemetry never raises into your code and never blocks it on the network:
  export runs on a background thread with a bounded queue
  ([overhead measurements](../../docs/benchmarks/sdk-overhead.md)).

## Install

The package is part of this repository's uv workspace
(`uv sync` at the repository root). To use it from another project:

```bash
pip install ./packages/sdk-python
```

## Configure

`agenttwin.configure()` reads the environment; keyword arguments win.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTTWIN_API_KEY` | – | Project API key (`atk_…`, scope `traces:write`). Determines the project server-side. |
| `AGENTTWIN_OTLP_ENDPOINT` | `http://localhost:4318` | Collector OTLP/HTTP base URL (`OTEL_EXPORTER_OTLP_ENDPOINT` also works). |
| `AGENTTWIN_API_URL` | – | Control-plane URL, needed for delayed outcomes and the REST client. |
| `AGENTTWIN_ENVIRONMENT` | `development` | Deployment environment of this process. |
| `AGENTTWIN_SOURCE` | `production` | `production`, `simulation`, `replay`, `eval` or `test`. |
| `AGENTTWIN_SERVICE_NAME`, `AGENTTWIN_RELEASE_ID`, `AGENTTWIN_COMMIT_SHA` | – | Resource metadata. |
| `AGENTTWIN_CONTENT_MODE` | `off` | `off`, `redacted` or `full` (the project policy is enforced again server-side). |
| `AGENTTWIN_REDACTION_STRATEGY` | `mask` | `mask`, `hash` or `drop`. |
| `AGENTTWIN_REDACTION_PATTERNS` | – | Extra regular expressions, one per line. |
| `AGENTTWIN_REDACTION_JSON_PATHS` | – | Comma-separated fields to redact, e.g. `$.customer.email`. |
| `AGENTTWIN_SAMPLE_RATIO` | `1.0` | Head sampling for new traces. |
| `AGENTTWIN_DISABLED` | – | `true` turns export off; spans are still created locally but never sent. |

## Instrument

Decorator style with the process-wide client:

```python
import agenttwin
from agenttwin import AgentTrace, tool_span

agenttwin.configure()


@tool_span(risk="READ")
def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered", "total": 140.0}


@tool_span(risk="WRITE_IRREVERSIBLE")  # the idempotency_key argument is recorded as a hash
def refund_payment(order_id: str, amount: float, idempotency_key: str) -> dict:
    return {"refund_id": "RF-1", "status": "succeeded"}


with AgentTrace(agent="support-refund-agent", version="1.2.4", input="Refund $40 for ORD-1001") as run:
    order = lookup_order("ORD-1001")
    refund_payment(order["order_id"], 40.0, idempotency_key="refund-ORD-1001-40.00")
    run.outcome("SUCCESS", business_outcome="REFUND_COMPLETED")
    print("trace id:", run.trace_id)

agenttwin.default_client().flush()
```

Explicit style (several clients, model calls, retrievals, policy decisions):

```python
from agenttwin import AgentTwin, Config

at = AgentTwin(Config.from_env(content_mode="redacted", environment="production"))
with at.agent_run("support-refund-agent", "1.3.0", input=user_message, session_id="sess-42") as run:
    with run.model_call(
        "anthropic",
        "claude-sonnet-5",
        input_messages=messages,
        prompt_hash=manifest.prompt_hash,
        prompt_version="1.3.0",
    ) as mc:
        response = client.messages.create(...)
        mc.record_response(
            output_messages=[...],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reasons=["tool_use"],
        )
    with run.retrieval("support-kb", query="refund window") as r:
        r.set_documents(docs)
    run.policy_decision("allow", policy="refunds", rule="under_limit", tool="refund_payment")
    with run.tool_call(
        "refund_payment", args=args, risk="WRITE_IRREVERSIBLE", idempotency_key=args["idempotency_key"]
    ) as call:
        call.set_result(result)
    run.outcome("SUCCESS", claimed="SUCCESS", business_outcome="REFUND_COMPLETED")
```

### Outcomes: claimed vs verified

An agent's own final answer is **not** evidence. Leave
`verification_source="unavailable"` (the default) unless an independent check
backs the status — for example a state assertion against your database:

```python
run.outcome(
    "FAILURE",
    claimed="SUCCESS",
    verified=True,
    verification_source="state_assertion",
    state_diff={"refund_count": 2},
)
```

Outcomes known only later (a refund settles, a customer replies) are recorded
through the API with the project key:

```python
from agenttwin import report_outcome

report_outcome(
    trace_id,
    "FAILURE",
    verified=True,
    verification_source="external_callback",
    claimed_status="SUCCESS",
    actual_state={"refund_count": 2},
)
```

A verified outcome that contradicts the agent's claim is flagged
(`contradiction`) in the trace explorer.

## REST client

`agenttwin.Client` covers what an agent repository or its CI needs:

```python
from agenttwin import Client

api = Client.from_config()  # AGENTTWIN_API_URL + AGENTTWIN_API_KEY
project = api.project_id("support")  # slug or id
api.register_manifest(project, open("agent.yaml").read(), commit_sha="0a1b2c3d", branch="main")
```

Manifest versions are immutable: re-registering identical content is a no-op,
changed content under an existing version fails with `VERSION_EXISTS`. Errors
are `agenttwin.APIError` with the API's machine-readable `code`; the key never
appears in errors or `repr`.

## Guarantees and limits

* Exceptions inside instrumentation are swallowed and logged at debug level;
  exceptions raised by *your* tool are recorded on the span and re-raised.
* When the collector is unreachable or slow, spans beyond `max_queue_size`
  (2048) are dropped and counted in `client.stats`; the agent is never blocked.
* Content attributes are capped at 8 KiB each (`max_content_bytes`).
* Secrets (private keys, JWTs, bearer tokens, API keys, `password=`-style
  credentials) are redacted even in `full` mode; `redacted` also masks e-mail
  addresses, phone numbers and (Luhn-valid) card numbers.

Run the SDK tests with `uv run pytest packages/sdk-python/tests`.
