// Package model is the trace-service's internal, semconv-independent schema.
// Business logic reads only these types; raw OTel attribute names stay in
// package semconv (ADR-0003).
package model

import "time"

// Kind is the normalized span kind.
type Kind string

const (
	KindAgent     Kind = "agent"
	KindModel     Kind = "model"
	KindTool      Kind = "tool"
	KindRetrieval Kind = "retrieval"
	KindPolicy    Kind = "policy"
	KindOutcome   Kind = "outcome"
	KindHTTP      Kind = "http"
	KindMCP       Kind = "mcp"
	KindOther     Kind = "other"
)

// ValidKind reports whether k is a known kind.
func ValidKind(k Kind) bool {
	switch k {
	case KindAgent, KindModel, KindTool, KindRetrieval, KindPolicy, KindOutcome, KindHTTP, KindMCP, KindOther:
		return true
	}
	return false
}

// Status is the normalized span status.
type Status string

const (
	StatusOK    Status = "OK"
	StatusError Status = "ERROR"
	StatusUnset Status = "UNSET"
)

// Resource holds trace-level metadata taken from resource attributes.
type Resource struct {
	ServiceName     string `json:"service_name,omitempty"`
	Environment     string `json:"environment,omitempty"`
	ProjectHint     string `json:"project_hint,omitempty"`
	AgentName       string `json:"agent_name,omitempty"`
	AgentVersion    string `json:"agent_version,omitempty"`
	ReleaseID       string `json:"release_id,omitempty"`
	CommitSHA       string `json:"commit_sha,omitempty"`
	SDKName         string `json:"sdk_name,omitempty"`
	SDKVersion      string `json:"sdk_version,omitempty"`
	Source          string `json:"source,omitempty"`
	ContentMode     string `json:"content_mode,omitempty"` // declared by the SDK
	ContentRedacted bool   `json:"content_redacted,omitempty"`
}

// Attrs are the normalized span facts. Pointer fields distinguish "absent"
// from zero values.
type Attrs struct {
	Operation string `json:"operation,omitempty"`

	// Agent
	AgentName    string `json:"agent_name,omitempty"`
	AgentVersion string `json:"agent_version,omitempty"`
	AgentID      string `json:"agent_id,omitempty"`
	SessionID    string `json:"session_id,omitempty"`

	// Trace-scoped context an SDK may set per span (overrides the resource
	// so one process can run several agents, versions or run sources).
	Environment string `json:"environment,omitempty"`
	Source      string `json:"source,omitempty"`
	ReleaseID   string `json:"release_id,omitempty"`
	CommitSHA   string `json:"commit_sha,omitempty"`

	// Model call
	Provider      string   `json:"provider,omitempty"`
	RequestModel  string   `json:"request_model,omitempty"`
	ResponseModel string   `json:"response_model,omitempty"`
	InputTokens   *int64   `json:"input_tokens,omitempty"`
	OutputTokens  *int64   `json:"output_tokens,omitempty"`
	Temperature   *float64 `json:"temperature,omitempty"`
	MaxTokens     *int64   `json:"max_tokens,omitempty"`
	FinishReasons []string `json:"finish_reasons,omitempty"`
	CostUSD       *float64 `json:"cost_usd,omitempty"`
	PromptHash    string   `json:"prompt_hash,omitempty"`
	PromptVersion string   `json:"prompt_version,omitempty"`

	// Tool call
	ToolName         string `json:"tool_name,omitempty"`
	ToolVersion      string `json:"tool_version,omitempty"`
	ToolCallID       string `json:"tool_call_id,omitempty"`
	ToolRisk         string `json:"tool_risk,omitempty"`
	ToolArgsHash     string `json:"tool_args_hash,omitempty"`
	ToolResultStatus string `json:"tool_result_status,omitempty"`
	IdempotencyKey   string `json:"idempotency_key_hash,omitempty"`
	Attempt          *int64 `json:"attempt,omitempty"`

	// Retrieval
	RetrievalSource string `json:"retrieval_source,omitempty"`
	DocumentCount   *int64 `json:"document_count,omitempty"`

	// Errors
	ErrorType        string `json:"error_type,omitempty"`
	ExceptionType    string `json:"exception_type,omitempty"`
	ExceptionMessage string `json:"exception_message,omitempty"`

	// Policy decision
	PolicyDecision string `json:"policy_decision,omitempty"`
	PolicyName     string `json:"policy_name,omitempty"`
	PolicyVersion  string `json:"policy_version,omitempty"`
	PolicyRule     string `json:"policy_rule,omitempty"`

	// Outcome report
	OutcomeStatus      string `json:"outcome_status,omitempty"`
	OutcomeClaimed     string `json:"outcome_claimed,omitempty"`
	BusinessOutcome    string `json:"business_outcome,omitempty"`
	OutcomeVerified    *bool  `json:"outcome_verified,omitempty"`
	VerificationSource string `json:"verification_source,omitempty"`

	// HTTP / MCP client spans
	HTTPMethod string `json:"http_method,omitempty"`
	HTTPHost   string `json:"http_host,omitempty"`
	HTTPStatus *int64 `json:"http_status,omitempty"`
	MCPMethod  string `json:"mcp_method,omitempty"`
	MCPServer  string `json:"mcp_server,omitempty"`

	// Simulation linkage
	SimulationRunID string `json:"simulation_run_id,omitempty"`
	ScenarioID      string `json:"scenario_id,omitempty"`

	// FinalStateDiff is a structured state change reported by the agent
	// adapter or tool twin (key -> [before, after]).
	FinalStateDiff map[string]any `json:"final_state_diff,omitempty"`

	// Extra carries remaining non-content attributes (bounded, sanitized).
	Extra map[string]any `json:"extra,omitempty"`
}

// Content holds captured content, present only when the project allows it.
type Content struct {
	Input string `json:"input,omitempty"`
	// InputContext is the request context the run acted in (tenant,
	// customer, ...) as the SDK recorded it: JSON text.
	InputContext       string `json:"input_context,omitempty"`
	Output             string `json:"output,omitempty"`
	SystemInstructions string `json:"system_instructions,omitempty"`
	ToolArgs           string `json:"tool_args,omitempty"`
	ToolResult         string `json:"tool_result,omitempty"`
}

// Empty reports whether no content is present.
func (c *Content) Empty() bool {
	return c == nil || (c.Input == "" && c.InputContext == "" && c.Output == "" && c.SystemInstructions == "" &&
		c.ToolArgs == "" && c.ToolResult == "")
}

// Event is a sanitized span event.
type Event struct {
	Name  string         `json:"name"`
	Time  time.Time      `json:"time"`
	Attrs map[string]any `json:"attributes,omitempty"`
}

// Span is a normalized span.
type Span struct {
	TraceID        string    `json:"trace_id"`
	SpanID         string    `json:"span_id"`
	ParentSpanID   string    `json:"parent_span_id,omitempty"`
	Name           string    `json:"name"`
	Kind           Kind      `json:"kind"`
	OTelKind       int       `json:"otel_kind"`
	Status         Status    `json:"status"`
	StatusMessage  string    `json:"status_message,omitempty"`
	Start          time.Time `json:"started_at"`
	End            time.Time `json:"ended_at"`
	Attrs          Attrs     `json:"attributes"`
	Events         []Event   `json:"events,omitempty"`
	Content        *Content  `json:"content,omitempty"`
	Resource       Resource  `json:"-"`
	SemconvVersion string    `json:"semconv_version"`
	Truncated      bool      `json:"truncated,omitempty"`
	ContentDropped bool      `json:"-"`
}

// DurationMS returns the span duration in milliseconds.
func (s Span) DurationMS() float64 { return float64(s.End.Sub(s.Start).Microseconds()) / 1000 }

// IsRoot reports whether the span has no parent.
func (s Span) IsRoot() bool { return s.ParentSpanID == "" }

// Model returns the most specific model name.
func (s Span) Model() string {
	if s.Attrs.ResponseModel != "" {
		return s.Attrs.ResponseModel
	}
	return s.Attrs.RequestModel
}
