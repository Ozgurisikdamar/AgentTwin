package summary

import (
	"reflect"
	"testing"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
)

var t0 = time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)

func sp(id, parent string, kind model.Kind, offsetMS int, a model.Attrs, status model.Status) model.Span {
	return model.Span{TraceID: "0af7651916cd43dd8448eb211c80319c", SpanID: id, ParentSpanID: parent, Name: string(kind), Kind: kind,
		Status: status, Start: t0.Add(time.Duration(offsetMS) * time.Millisecond), End: t0.Add(time.Duration(offsetMS+50) * time.Millisecond),
		Attrs: a, SemconvVersion: "none"}
}

func f(v float64) *float64 { return &v }
func i(v int64) *int64     { return &v }
func b(v bool) *bool       { return &v }

// candidateTrace reproduces the demo regression: refund times out after the
// mutation, the agent retries without verifying and refunds twice.
func candidateTrace() []model.Span {
	root := sp("a000000000000001", "", model.KindAgent, 0, model.Attrs{AgentName: "support-refund-agent", AgentVersion: "1.3.0"}, model.StatusOK)
	root.End = t0.Add(2 * time.Second)
	return []model.Span{
		root,
		sp("a000000000000002", root.SpanID, model.KindModel, 10, model.Attrs{RequestModel: "scripted-planner-v1", InputTokens: i(900), OutputTokens: i(40), CostUSD: f(0.0021), PromptHash: "p1"}, model.StatusOK),
		sp("a000000000000003", root.SpanID, model.KindTool, 100, model.Attrs{ToolName: "lookup_order", ToolRisk: "READ", ToolArgsHash: "h-o"}, model.StatusOK),
		sp("a000000000000004", root.SpanID, model.KindTool, 200, model.Attrs{ToolName: "refund_payment", ToolRisk: "WRITE_IRREVERSIBLE", ToolArgsHash: "h-r", ToolResultStatus: "timeout"}, model.StatusError),
		sp("a000000000000005", root.SpanID, model.KindTool, 400, model.Attrs{ToolName: "refund_payment", ToolRisk: "WRITE_IRREVERSIBLE", ToolArgsHash: "h-r", ToolResultStatus: "ok"}, model.StatusOK),
		sp("a000000000000006", "a000000000000005", model.KindHTTP, 410, model.Attrs{HTTPMethod: "POST", HTTPHost: "payments.internal"}, model.StatusOK),
		sp("a000000000000007", root.SpanID, model.KindOutcome, 1900, model.Attrs{OutcomeStatus: "FAILURE", OutcomeClaimed: "SUCCESS", OutcomeVerified: b(true),
			VerificationSource: "tool_twin_state", FinalStateDiff: map[string]any{"refund_count": []any{0, 2}}}, model.StatusOK),
	}
}

func TestCandidateRegressionSummary(t *testing.T) {
	r := Build(candidateTrace(), nil, nil)
	s := r.Summary
	if !reflect.DeepEqual(s.Tools, []string{"lookup_order", "refund_payment", "refund_payment"}) {
		t.Fatalf("tools %v", s.Tools)
	}
	if !reflect.DeepEqual(s.Errors, []string{"refund_payment:timeout", "timeout_after_mutation"}) {
		t.Fatalf("errors %v", s.Errors)
	}
	if !reflect.DeepEqual(s.Violations, []string{"retry_without_idempotency_key", "duplicate_irreversible_action"}) {
		t.Fatalf("violations %v", s.Violations)
	}
	if *s.Outcome != "FAILURE" || r.SpanOutcome == nil || !r.SpanOutcome.Contradiction {
		t.Fatalf("outcome %+v", r.SpanOutcome)
	}
	wantSignals := []string{"contradiction", "duplicate_side_effect", "error", "outcome_failure", "retry", "timeout_after_mutation", "tool_error"}
	if !reflect.DeepEqual(r.Signals, wantSignals) {
		t.Fatalf("signals %v", r.Signals)
	}
	if r.RetryCount != 1 || r.ToolCalls != 3 || r.ModelCalls != 1 || s.StepCount != 4 || *r.CostUSD != 0.0021 {
		t.Fatalf("aggregates retry=%d tools=%d models=%d steps=%d cost=%v", r.RetryCount, r.ToolCalls, r.ModelCalls, s.StepCount, *r.CostUSD)
	}
	if s.ToolSequenceSketch != "lookup_order>refund_payment x2" || s.FailingTool != "refund_payment" || s.ErrorType != "timeout" {
		t.Fatalf("features %+v", s)
	}
	if s.FinalStateDiff["refund_count"] == nil || r.AgentVersion != "1.3.0" || r.DurationMS != 2000 {
		t.Fatalf("state diff / version / duration: %+v %s %v", s.FinalStateDiff, r.AgentVersion, r.DurationMS)
	}
	if len(r.ObservedTools) != 2 || r.ObservedTools[1].HTTPHost != "payments.internal" || r.ObservedTools[1].Count != 2 {
		t.Fatalf("observed tools %+v", r.ObservedTools)
	}
}

func TestSummaryIsOrderIndependent(t *testing.T) {
	spans := candidateTrace()
	a := Build(spans, nil, nil)
	rev := make([]model.Span, len(spans))
	for k := range spans {
		rev[len(spans)-1-k] = spans[k]
	}
	if bb := Build(rev, nil, nil); !reflect.DeepEqual(a, bb) {
		t.Fatalf("summary depends on span order:\n%+v\n%+v", a, bb)
	}
}

func TestUnverifiableOutcomeIsNeverVerified(t *testing.T) {
	spans := []model.Span{sp("a000000000000001", "", model.KindAgent, 0, model.Attrs{}, model.StatusOK),
		sp("a000000000000002", "a000000000000001", model.KindOutcome, 5, model.Attrs{OutcomeStatus: "SUCCESS", OutcomeVerified: b(true), VerificationSource: "unavailable"}, model.StatusOK)}
	r := Build(spans, nil, nil)
	if r.SpanOutcome.Verified || !contains(r.Signals, "outcome_unverified") {
		t.Fatalf("a claim without verification source must stay unverified: %+v %v", r.SpanOutcome, r.Signals)
	}
}

func TestPricingEstimateAndIncompleteTrace(t *testing.T) {
	spans := []model.Span{sp("a000000000000002", "a000000000000001", model.KindModel, 0,
		model.Attrs{RequestModel: "m", InputTokens: i(1_000_000), OutputTokens: i(500_000)}, model.StatusOK)}
	r := Build(spans, nil, Pricing{"m": {InputPerMTok: 3, OutputPerMTok: 15}})
	if r.CostUSD == nil || *r.CostUSD != 10.5 {
		t.Fatalf("estimated cost %v", r.CostUSD)
	}
	if !contains(r.Signals, "incomplete_trace") || r.RootReceived {
		t.Fatalf("trace without root must be flagged incomplete: %v", r.Signals)
	}
}

func TestPolicyDenyAndApproval(t *testing.T) {
	spans := []model.Span{
		sp("a000000000000001", "", model.KindAgent, 0, model.Attrs{}, model.StatusOK),
		sp("a000000000000002", "a000000000000001", model.KindTool, 5, model.Attrs{ToolName: "refund_payment"}, model.StatusError),
		sp("a000000000000003", "a000000000000002", model.KindPolicy, 6, model.Attrs{PolicyDecision: "deny"}, model.StatusOK),
		sp("a000000000000004", "a000000000000001", model.KindPolicy, 9, model.Attrs{PolicyDecision: "require_approval", ToolName: "send_email"}, model.StatusOK),
	}
	r := Build(spans, nil, nil)
	if !reflect.DeepEqual(r.Summary.Violations, []string{"policy_denied:refund_payment"}) || !contains(r.Signals, "policy_denied") || !contains(r.Signals, "approval_required") {
		t.Fatalf("policy: %v %v", r.Summary.Violations, r.Signals)
	}
	if !reflect.DeepEqual(r.PolicyDecisions, []string{"deny", "require_approval"}) {
		t.Fatalf("decisions %v", r.PolicyDecisions)
	}
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func TestLastSuccessfulStepNamesTheStep(t *testing.T) {
	root := sp("b000000000000001", "", model.KindAgent, 0, model.Attrs{AgentName: "a"}, model.StatusOK)
	spans := []model.Span{
		root,
		sp("b000000000000002", root.SpanID, model.KindTool, 10, model.Attrs{ToolName: "lookup_order", ToolRisk: "READ"}, model.StatusOK),
		sp("b000000000000003", root.SpanID, model.KindModel, 100, model.Attrs{RequestModel: "m-req", ResponseModel: "m-resp"}, model.StatusOK),
		sp("b000000000000004", root.SpanID, model.KindTool, 200, model.Attrs{ToolName: "refund_payment", ToolRisk: "WRITE_IRREVERSIBLE", ToolResultStatus: "error"}, model.StatusError),
	}
	if got := Build(spans, nil, nil).Summary.LastSuccessfulStep; got != "model:m-resp" {
		t.Fatalf("last successful step %q, want model:m-resp", got)
	}
	if got := Build(candidateTrace(), nil, nil).Summary.LastSuccessfulStep; got != "tool:refund_payment" {
		t.Fatalf("candidate last successful step %q", got)
	}
}
