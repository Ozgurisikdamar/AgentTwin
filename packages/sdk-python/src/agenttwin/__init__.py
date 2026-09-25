"""AgentTwin Python SDK.

Quick start::

    import agenttwin
    from agenttwin import AgentTrace, tool_span

    agenttwin.configure()  # reads AGENTTWIN_API_KEY, AGENTTWIN_OTLP_ENDPOINT, ...

    @tool_span(name="refund_payment", risk="WRITE_IRREVERSIBLE")
    def refund_payment(order_id: str, amount: float, idempotency_key: str) -> dict: ...

    with AgentTrace(agent="refund-agent", version="1.3.0") as trace:
        refund_payment("ORD-1", 50.0, idempotency_key="refund-ORD-1")
        trace.outcome("SUCCESS", business_outcome="REFUND_COMPLETED")

Content capture is off by default; see ``Config.content_mode``.
"""

from agenttwin._version import __version__
from agenttwin.api import APIError, Client
from agenttwin.config import Config
from agenttwin.gateway import Gateway, GatewayError, ToolResponse
from agenttwin.hashing import canonical_json, content_hash, sha256_hex
from agenttwin.manifest import AgentManifest, load_manifest
from agenttwin.outcomes import OutcomeReportError, report_outcome
from agenttwin.redaction import RedactionConfig, Redactor
from agenttwin.tracing import (
    AgentRun,
    AgentTrace,
    AgentTwin,
    ExportStats,
    ModelCall,
    Retrieval,
    ToolCall,
    configure,
    current_run,
    default_client,
    outcome,
    tool_span,
)

__all__ = [
    "APIError",
    "AgentManifest",
    "AgentRun",
    "AgentTrace",
    "AgentTwin",
    "Client",
    "Config",
    "ExportStats",
    "Gateway",
    "GatewayError",
    "ModelCall",
    "OutcomeReportError",
    "RedactionConfig",
    "Redactor",
    "Retrieval",
    "ToolCall",
    "ToolResponse",
    "__version__",
    "canonical_json",
    "configure",
    "content_hash",
    "current_run",
    "default_client",
    "load_manifest",
    "outcome",
    "report_outcome",
    "sha256_hex",
    "tool_span",
]
