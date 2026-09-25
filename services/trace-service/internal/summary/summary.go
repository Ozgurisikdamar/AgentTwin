// Package summary derives the deterministic structured summary of a trace
// (spec §45): aggregates for the explorer, failure features and signals for
// the regression miner. It never embeds or interprets raw content.
package summary

import (
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
)

// Outcome is an outcome reported on a span or recorded through the API.
type Outcome struct {
	Status             string `json:"status"`
	BusinessOutcome    string `json:"business_outcome,omitempty"`
	Verified           bool   `json:"verified"`
	VerificationSource string `json:"verification_source"`
	Claimed            string `json:"claimed_status,omitempty"`
	Contradiction      bool   `json:"contradiction"`
}

// Summary is stored on the trace and published in trace.ingested.v1.
type Summary struct {
	Agent              string         `json:"agent"`
	AgentVersion       string         `json:"agent_version,omitempty"`
	Outcome            *string        `json:"outcome"`
	OutcomeVerified    *bool          `json:"outcome_verified,omitempty"`
	Tools              []string       `json:"tools"`
	Errors             []string       `json:"errors"`
	Violations         []string       `json:"violations"`
	PolicyDecisions    []string       `json:"policy_decisions"`
	StepCount          int            `json:"step_count"`
	RetryCount         int            `json:"retry_count"`
	CostUSD            float64        `json:"cost_usd"`
	CostKnown          bool           `json:"cost_known"`
	DurationMS         float64        `json:"duration_ms"`
	Model              *string        `json:"model"`
	PromptHash         *string        `json:"prompt_hash"`
	FinalStateDiff     map[string]any `json:"final_state_diff,omitempty"`
	FailingTool        string         `json:"failing_tool,omitempty"`
	LastSuccessfulStep string         `json:"last_successful_step,omitempty"`
	ErrorType          string         `json:"error_type,omitempty"`
	ToolSequenceSketch string         `json:"tool_sequence_sketch"`
}

// ObservedTool is a tool seen in the trace (graph OBSERVED evidence).
type ObservedTool struct {
	Name     string `json:"name"`
	Risk     string `json:"risk,omitempty"`
	HTTPHost string `json:"http_host,omitempty"`
	Count    int    `json:"count"`
}

// Result aggregates a trace.
type Result struct {
	Status          model.Status
	RootSpanID      string
	RootName        string
	RootReceived    bool
	AgentName       string
	AgentVersion    string
	SessionID       string
	Environment     string
	StartedAt       time.Time
	EndedAt         time.Time
	DurationMS      float64
	SpanCount       int
	ModelCalls      int
	ToolCalls       int
	ErrorCount      int
	RetryCount      int
	InputTokens     int64
	OutputTokens    int64
	CostUSD         *float64
	Models          []string
	Tools           []string
	PolicyDecisions []string
	Signals         []string
	PromptHash      string
	SemconvVersion  string
	Summary         Summary
	ObservedTools   []ObservedTool
	SpanOutcome     *Outcome
}

// Pricing estimates model cost from tokens when spans do not report it.
type Pricing map[string]struct {
	InputPerMTok  float64 `json:"input_per_mtok"`
	OutputPerMTok float64 `json:"output_per_mtok"`
}

var failedResults = map[string]bool{"error": true, "timeout": true, "rate_limited": true, "denied": true, "invalid": true}

// isWrite reports whether a tool risk level (spec §81: READ,
// WRITE_REVERSIBLE, WRITE_IRREVERSIBLE, EXECUTE, ADMIN) can change state.
func isWrite(risk string) bool {
	return strings.HasPrefix(risk, "WRITE_") || risk == "EXECUTE" || risk == "ADMIN"
}

// Build computes the summary. Spans may be in any order; the result is
// deterministic for the same span set.
func Build(spans []model.Span, apiOutcome *Outcome, pricing Pricing) Result {
	ordered := append([]model.Span(nil), spans...)
	sort.Slice(ordered, func(i, j int) bool {
		if !ordered[i].Start.Equal(ordered[j].Start) {
			return ordered[i].Start.Before(ordered[j].Start)
		}
		return ordered[i].SpanID < ordered[j].SpanID
	})
	var res Result
	res.SpanCount = len(ordered)
	res.Status = model.StatusUnset
	byID := map[string]model.Span{}
	for _, s := range ordered {
		byID[s.SpanID] = s
	}
	models := map[string]bool{}
	decisions := map[string]bool{}
	versions := map[string]bool{}
	var root *model.Span
	var sum Summary
	sum.Tools, sum.Errors, sum.Violations, sum.PolicyDecisions = []string{}, []string{}, []string{}, []string{}
	errSeen, violSeen := map[string]bool{}, map[string]bool{}
	addErr := func(e string) {
		if !errSeen[e] {
			errSeen[e] = true
			sum.Errors = append(sum.Errors, e)
		}
	}
	addViol := func(v string) {
		if !violSeen[v] {
			violSeen[v] = true
			sum.Violations = append(sum.Violations, v)
		}
	}
	type callKey struct{ tool, args string }
	callCounts := map[callKey]int{}
	lastResult := map[string]string{} // tool -> result of its previous call
	lastArgs := map[string]string{}
	observed := map[string]*ObservedTool{}
	cost, costKnown := 0.0, false

	for i := range ordered {
		s := ordered[i]
		versions[s.SemconvVersion] = true
		if res.StartedAt.IsZero() || s.Start.Before(res.StartedAt) {
			res.StartedAt = s.Start
		}
		if s.End.After(res.EndedAt) {
			res.EndedAt = s.End
		}
		if s.IsRoot() && root == nil {
			r := ordered[i]
			root = &r
		}
		if s.Status == model.StatusError {
			res.ErrorCount++
		}
		if s.Attrs.CostUSD != nil {
			cost += *s.Attrs.CostUSD
			costKnown = true
		}
		if s.Attrs.PromptHash != "" && res.PromptHash == "" {
			res.PromptHash = s.Attrs.PromptHash
		}
		if s.Attrs.SessionID != "" && res.SessionID == "" {
			res.SessionID = s.Attrs.SessionID
		}
		if s.Attrs.FinalStateDiff != nil {
			sum.FinalStateDiff = s.Attrs.FinalStateDiff
		}
		switch s.Kind {
		case model.KindModel:
			res.ModelCalls++
			if m := s.Model(); m != "" {
				models[m] = true
			}
			if s.Attrs.InputTokens != nil {
				res.InputTokens += *s.Attrs.InputTokens
			}
			if s.Attrs.OutputTokens != nil {
				res.OutputTokens += *s.Attrs.OutputTokens
			}
			if s.Attrs.CostUSD == nil {
				if p, ok := pricing[s.Model()]; ok && (s.Attrs.InputTokens != nil || s.Attrs.OutputTokens != nil) {
					in, out := int64(0), int64(0)
					if s.Attrs.InputTokens != nil {
						in = *s.Attrs.InputTokens
					}
					if s.Attrs.OutputTokens != nil {
						out = *s.Attrs.OutputTokens
					}
					cost += float64(in)/1e6*p.InputPerMTok + float64(out)/1e6*p.OutputPerMTok
					costKnown = true
				}
			}
			if s.Status == model.StatusError {
				addErr("model:" + firstNonEmpty(s.Attrs.ErrorType, s.Attrs.ExceptionType, "error"))
			} else {
				sum.LastSuccessfulStep = "model:" + firstNonEmpty(s.Model(), s.Name)
			}
		case model.KindTool:
			res.ToolCalls++
			name := firstNonEmpty(s.Attrs.ToolName, s.Name)
			sum.Tools = append(sum.Tools, name)
			result := s.Attrs.ToolResultStatus
			if result == "" {
				if s.Status == model.StatusError {
					result = "error"
				} else {
					result = "ok"
				}
			}
			failed := failedResults[result] || s.Status == model.StatusError
			key := callKey{name, s.Attrs.ToolArgsHash}
			retry := (s.Attrs.Attempt != nil && *s.Attrs.Attempt > 1) ||
				(failedResults[lastResult[name]] && lastArgs[name] == s.Attrs.ToolArgsHash)
			if retry {
				res.RetryCount++
				if isWrite(s.Attrs.ToolRisk) && s.Attrs.IdempotencyKey == "" {
					addViol("retry_without_idempotency_key")
				}
			}
			// A mutation whose outcome is unknown (timeout) or confirmed (ok)
			// counts as a possibly executed side effect.
			if result == "ok" || result == "timeout" {
				callCounts[key]++
			}
			if failed {
				addErr(name + ":" + firstNonEmpty(nonOK(result), s.Attrs.ErrorType, "error"))
				if sum.FailingTool == "" {
					sum.FailingTool = name
					sum.ErrorType = firstNonEmpty(nonOK(result), s.Attrs.ErrorType, s.Attrs.ExceptionType, "error")
				}
				if result == "timeout" && isWrite(s.Attrs.ToolRisk) {
					addErr("timeout_after_mutation")
				}
			} else {
				sum.LastSuccessfulStep = "tool:" + name
			}
			lastResult[name], lastArgs[name] = result, s.Attrs.ToolArgsHash
			ot := observed[name]
			if ot == nil {
				ot = &ObservedTool{Name: name}
				observed[name] = ot
			}
			ot.Count++
			if s.Attrs.ToolRisk != "" {
				ot.Risk = s.Attrs.ToolRisk
			}
		case model.KindPolicy:
			d := s.Attrs.PolicyDecision
			if d != "" {
				decisions[d] = true
				if d == "deny" {
					addViol("policy_denied:" + firstNonEmpty(s.Attrs.ToolName, toolOfParent(byID, s), "unknown"))
				}
			}
		case model.KindHTTP, model.KindMCP:
			// Attribute downstream hosts to the calling tool.
			if s.Attrs.HTTPHost != "" {
				if t := toolOfParent(byID, s); t != "" {
					if ot := observed[t]; ot != nil && ot.HTTPHost == "" {
						ot.HTTPHost = s.Attrs.HTTPHost
					}
				}
			}
			if s.Status == model.StatusError {
				addErr(string(s.Kind) + ":" + firstNonEmpty(s.Attrs.ErrorType, "error"))
			}
		case model.KindOutcome:
			if s.Attrs.OutcomeStatus != "" {
				res.SpanOutcome = outcomeFromSpan(s)
			}
		}
		if s.Kind != model.KindOutcome && s.Attrs.OutcomeStatus != "" && res.SpanOutcome == nil {
			res.SpanOutcome = outcomeFromSpan(s)
		}
	}
	// Irreversible tools that may have executed more than once with the
	// same arguments.
	dupKeys := make([]callKey, 0)
	for k, n := range callCounts {
		if n >= 2 {
			dupKeys = append(dupKeys, k)
		}
	}
	sort.Slice(dupKeys, func(i, j int) bool {
		return dupKeys[i].tool < dupKeys[j].tool || (dupKeys[i].tool == dupKeys[j].tool && dupKeys[i].args < dupKeys[j].args)
	})
	loop := false
	for _, k := range dupKeys {
		if risk := observed[k.tool].Risk; risk == "WRITE_IRREVERSIBLE" {
			addViol("duplicate_irreversible_action")
		}
		if callCounts[k] >= 3 {
			loop = true
		}
	}

	if root != nil {
		res.RootReceived = true
		res.RootSpanID, res.RootName = root.SpanID, root.Name
		res.Status = root.Status
		res.AgentName = firstNonEmpty(root.Attrs.AgentName, root.Resource.AgentName)
		res.AgentVersion = firstNonEmpty(root.Attrs.AgentVersion, root.Resource.AgentVersion)
		res.Environment = firstNonEmpty(root.Attrs.Environment, root.Resource.Environment)
		if root.Status == model.StatusError {
			addErr("agent:" + firstNonEmpty(root.Attrs.ErrorType, root.Attrs.ExceptionType, "error"))
		}
	}
	for _, s := range ordered {
		if res.AgentName == "" {
			res.AgentName = firstNonEmpty(s.Attrs.AgentName, s.Resource.AgentName)
		}
		if res.AgentVersion == "" {
			res.AgentVersion = firstNonEmpty(s.Attrs.AgentVersion, s.Resource.AgentVersion)
		}
		if res.Environment == "" {
			res.Environment = firstNonEmpty(s.Attrs.Environment, s.Resource.Environment)
		}
	}
	if !res.StartedAt.IsZero() {
		res.DurationMS = float64(res.EndedAt.Sub(res.StartedAt).Microseconds()) / 1000
	}
	if costKnown {
		c := round6(cost)
		res.CostUSD = &c
	}
	res.Models = sortedKeys(models)
	res.PolicyDecisions = sortedKeys(decisions)
	res.Tools = sortedKeys(func() map[string]bool {
		m := map[string]bool{}
		for _, t := range sum.Tools {
			m[t] = true
		}
		return m
	}())
	delete(versions, "")
	res.SemconvVersion = semconvLabel(versions)

	outcome := res.SpanOutcome
	if apiOutcome != nil {
		outcome = apiOutcome
	}
	// Assemble the published summary.
	sum.Agent = res.AgentName
	sum.AgentVersion = res.AgentVersion
	sum.PolicyDecisions = res.PolicyDecisions
	if outcome != nil {
		st := outcome.Status
		sum.Outcome = &st
		v := outcome.Verified
		sum.OutcomeVerified = &v
	}
	sum.StepCount = res.ModelCalls + res.ToolCalls
	sum.RetryCount = res.RetryCount
	sum.CostUSD = 0
	if res.CostUSD != nil {
		sum.CostUSD = *res.CostUSD
		sum.CostKnown = true
	}
	sum.DurationMS = res.DurationMS
	if len(res.Models) > 0 {
		m := res.Models[0]
		sum.Model = &m
	}
	if res.PromptHash != "" {
		ph := res.PromptHash
		sum.PromptHash = &ph
	}
	sum.ToolSequenceSketch = sketch(sum.Tools)
	res.Summary = sum

	// Deterministic signals for the regression miner.
	sig := map[string]bool{}
	if res.ErrorCount > 0 {
		sig["error"] = true
	}
	for _, e := range sum.Errors {
		switch {
		case strings.HasPrefix(e, "model:"):
			sig["model_error"] = true
		case strings.HasPrefix(e, "agent:"), strings.HasPrefix(e, "http:"), strings.HasPrefix(e, "mcp:"):
		case e == "timeout_after_mutation":
			sig["timeout_after_mutation"] = true
		default:
			sig["tool_error"] = true
		}
	}
	if res.RetryCount > 0 {
		sig["retry"] = true
	}
	if decisions["deny"] {
		sig["policy_denied"] = true
	}
	if decisions["require_approval"] {
		sig["approval_required"] = true
	}
	if violSeen["duplicate_irreversible_action"] {
		sig["duplicate_side_effect"] = true
	}
	if loop {
		sig["loop_detected"] = true
	}
	if outcome != nil {
		switch outcome.Status {
		case "FAILURE", "PARTIAL":
			sig["outcome_failure"] = true
		}
		if !outcome.Verified {
			sig["outcome_unverified"] = true
		}
		if outcome.Contradiction {
			sig["contradiction"] = true
		}
	}
	if root == nil {
		sig["incomplete_trace"] = true
	}
	res.Signals = sortedKeys(sig)
	obs := make([]ObservedTool, 0, len(observed))
	for _, name := range res.Tools {
		obs = append(obs, *observed[name])
	}
	res.ObservedTools = obs
	return res
}

// OutcomeStatuses and VerificationSources are the accepted vocabularies
// (spec §15). Span attributes are untrusted input: anything else is
// normalized instead of being stored (or breaking the finalizer).
var (
	OutcomeStatuses     = map[string]bool{"SUCCESS": true, "PARTIAL": true, "FAILURE": true, "UNKNOWN": true}
	VerificationSources = map[string]bool{"state_assertion": true, "tool_twin_state": true, "tool_result": true,
		"external_callback": true, "human_review": true, "semantic_judge": true, "unavailable": true}
)

func outcomeFromSpan(s model.Span) *Outcome {
	o := &Outcome{Status: s.Attrs.OutcomeStatus, BusinessOutcome: s.Attrs.BusinessOutcome,
		VerificationSource: firstNonEmpty(s.Attrs.VerificationSource, "unavailable"), Claimed: s.Attrs.OutcomeClaimed}
	if !OutcomeStatuses[o.Status] {
		o.Status = "UNKNOWN"
	}
	if !OutcomeStatuses[o.Claimed] {
		o.Claimed = ""
	}
	if !VerificationSources[o.VerificationSource] {
		o.VerificationSource = "unavailable"
	}
	if len(o.BusinessOutcome) > 200 {
		cut := 200
		for cut > 0 && !utf8.RuneStart(o.BusinessOutcome[cut]) {
			cut--
		}
		o.BusinessOutcome = o.BusinessOutcome[:cut]
	}
	if s.Attrs.OutcomeVerified != nil {
		o.Verified = *s.Attrs.OutcomeVerified
	}
	// Spec §15: an unverifiable claim is never "verified".
	if o.VerificationSource == "unavailable" {
		o.Verified = false
	}
	o.Contradiction = Contradiction(o.Claimed, o.Status, o.Verified)
	return o
}

// Contradiction reports an agent claiming success that verification disproved.
func Contradiction(claimed, status string, verified bool) bool {
	return verified && claimed == "SUCCESS" && (status == "FAILURE" || status == "PARTIAL")
}

func toolOfParent(byID map[string]model.Span, s model.Span) string {
	seen := 0
	for p := s.ParentSpanID; p != "" && seen < 64; seen++ {
		ps, ok := byID[p]
		if !ok {
			return ""
		}
		if ps.Kind == model.KindTool {
			return firstNonEmpty(ps.Attrs.ToolName, ps.Name)
		}
		p = ps.ParentSpanID
	}
	return ""
}

// sketch compresses a call sequence: a>b>b>b>c becomes "a>b x3>c".
func sketch(tools []string) string {
	var parts []string
	for i := 0; i < len(tools); {
		j := i
		for j < len(tools) && tools[j] == tools[i] {
			j++
		}
		p := tools[i]
		if j-i > 1 {
			p += " x" + itoa(j-i)
		}
		parts = append(parts, p)
		i = j
	}
	return strings.Join(parts, ">")
}

func semconvLabel(versions map[string]bool) string {
	delete(versions, "none")
	if len(versions) == 0 {
		return "none"
	}
	return strings.Join(sortedKeys(versions), "+")
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

func nonOK(result string) string {
	if result == "ok" {
		return ""
	}
	return result
}

func round6(f float64) float64 {
	const scale = 1e6
	if f < 0 {
		return -round6(-f)
	}
	return float64(int64(f*scale+0.5)) / scale
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
