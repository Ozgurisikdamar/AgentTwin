// Package semconv maps OpenTelemetry attributes onto the internal schema
// through versioned, table-driven adapters (ADR-0003). This is the only
// package that knows raw attribute names.
package semconv

import (
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/otlp"
)

// Adapter describes one generation of GenAI semantic conventions. Attributes
// that did not change between generations are handled by the common mapping.
type Adapter struct {
	Version string
	// Markers identify spans written with this generation: any present key matches.
	Markers       []string
	Provider      []string
	InputTokens   []string
	OutputTokens  []string
	Session       []string
	InputContent  []string
	OutputContent []string
	SystemContent []string
	// ContentEvents maps an event name to the attribute carrying its content
	// and whether it is input (true) or output (false).
	ContentEvents map[string]contentEvent
	// ContentPrefixes are indexed attribute families (e.g. gen_ai.prompt.0.content).
	InputContentPrefixes  []string
	OutputContentPrefixes []string
}

type contentEvent struct {
	attr  string
	input bool
}

// V137 is the GenAI semantic conventions generation current at v1.37+
// (gen_ai.provider.name, input/output tokens, structured messages).
var V137 = Adapter{
	Version:       "genai-v1.37",
	Markers:       []string{"gen_ai.provider.name", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.conversation.id"},
	Provider:      []string{"gen_ai.provider.name"},
	InputTokens:   []string{"gen_ai.usage.input_tokens"},
	OutputTokens:  []string{"gen_ai.usage.output_tokens"},
	Session:       []string{"gen_ai.conversation.id"},
	InputContent:  []string{"gen_ai.input.messages"},
	OutputContent: []string{"gen_ai.output.messages"},
	SystemContent: []string{"gen_ai.system_instructions"},
}

// Legacy is the earlier generation (gen_ai.system, prompt/completion tokens,
// prompt/completion content attributes and events).
var Legacy = Adapter{
	Version:       "genai-legacy",
	Markers:       []string{"gen_ai.system", "gen_ai.usage.prompt_tokens", "gen_ai.usage.completion_tokens", "gen_ai.prompt", "gen_ai.completion", "gen_ai.prompt.0.content", "gen_ai.completion.0.content"},
	Provider:      []string{"gen_ai.system"},
	InputTokens:   []string{"gen_ai.usage.prompt_tokens"},
	OutputTokens:  []string{"gen_ai.usage.completion_tokens"},
	InputContent:  []string{"gen_ai.prompt"},
	OutputContent: []string{"gen_ai.completion"},
	ContentEvents: map[string]contentEvent{
		"gen_ai.content.prompt":     {attr: "gen_ai.prompt", input: true},
		"gen_ai.content.completion": {attr: "gen_ai.completion", input: false},
	},
	InputContentPrefixes:  []string{"gen_ai.prompt."},
	OutputContentPrefixes: []string{"gen_ai.completion."},
}

// Adapters in detection priority order.
var Adapters = []Adapter{V137, Legacy}

// None marks spans without GenAI attributes (AgentTwin-only or plain OTel).
const None = "none"

// contentKeys are attributes that may carry content in any generation. They
// never reach Extra, whatever the capture mode.
var contentKeys = map[string]bool{
	"gen_ai.input.messages": true, "gen_ai.output.messages": true, "gen_ai.system_instructions": true,
	"gen_ai.prompt": true, "gen_ai.completion": true, "gen_ai.tool.call.arguments": true, "gen_ai.tool.call.result": true,
	"agenttwin.input": true, "agenttwin.output": true, "agenttwin.tool.args": true, "agenttwin.tool.result": true,
	"gen_ai.tool.definitions": true,
}

var contentPrefixes = []string{"gen_ai.prompt.", "gen_ai.completion.", "llm.input_messages", "llm.output_messages", "input.value", "output.value"}

// IsContentKey reports whether an attribute may carry prompt/response content.
func IsContentKey(k string) bool {
	if contentKeys[k] {
		return true
	}
	for _, p := range contentPrefixes {
		if strings.HasPrefix(k, p) {
			return true
		}
	}
	return false
}

// Detect returns the adapter matching the span, or nil.
func Detect(attrs map[string]any, events []otlp.Event) *Adapter {
	for i := range Adapters {
		a := &Adapters[i]
		for _, m := range a.Markers {
			if _, ok := attrs[m]; ok {
				return a
			}
		}
		for _, ev := range events {
			if _, ok := a.ContentEvents[ev.Name]; ok {
				return a
			}
		}
	}
	return nil
}

// attrReader tracks which keys were consumed so the rest can go to Extra.
type attrReader struct {
	m    map[string]any
	used map[string]bool
}

func newReader(m map[string]any) *attrReader { return &attrReader{m: m, used: map[string]bool{}} }

func (r *attrReader) get(keys ...string) (any, bool) {
	for _, k := range keys {
		if v, ok := r.m[k]; ok {
			r.used[k] = true
			return v, true
		}
	}
	return nil, false
}

func (r *attrReader) str(keys ...string) string {
	v, ok := r.get(keys...)
	if !ok {
		return ""
	}
	return toString(v)
}

func (r *attrReader) int(keys ...string) *int64 {
	v, ok := r.get(keys...)
	if !ok {
		return nil
	}
	if n, ok := toInt(v); ok {
		return &n
	}
	return nil
}

func (r *attrReader) float(keys ...string) *float64 {
	v, ok := r.get(keys...)
	if !ok {
		return nil
	}
	if f, ok := toFloat(v); ok {
		return &f
	}
	return nil
}

// count reads a count or size: a negative value is not one, and is dropped
// rather than let into the aggregates.
func (r *attrReader) count(keys ...string) *int64 {
	if n := r.int(keys...); n != nil && *n >= 0 {
		return n
	}
	return nil
}

// amount reads a non-negative amount (a cost).
func (r *attrReader) amount(keys ...string) *float64 {
	if f := r.float(keys...); f != nil && *f >= 0 {
		return f
	}
	return nil
}

func (r *attrReader) bool(keys ...string) *bool {
	v, ok := r.get(keys...)
	if !ok {
		return nil
	}
	switch x := v.(type) {
	case bool:
		return &x
	case string:
		if b, err := strconv.ParseBool(x); err == nil {
			return &b
		}
	}
	return nil
}

func (r *attrReader) strings(keys ...string) []string {
	v, ok := r.get(keys...)
	if !ok {
		return nil
	}
	switch x := v.(type) {
	case []any:
		out := make([]string, 0, len(x))
		for _, e := range x {
			out = append(out, toString(e))
		}
		return out
	case string:
		return []string{x}
	}
	return nil
}

func toString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(x)
	case nil:
		return ""
	default:
		b, _ := json.Marshal(x)
		return string(b)
	}
}

func toInt(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case float64:
		if x == float64(int64(x)) {
			return int64(x), true
		}
	case string:
		n, err := strconv.ParseInt(x, 10, 64)
		return n, err == nil
	}
	return 0, false
}

// toFloat reads a finite number: NaN and infinities — which a string
// attribute can spell — have no JSON encoding and would fail the whole export.
func toFloat(v any) (float64, bool) {
	var f float64
	switch x := v.(type) {
	case float64:
		f = x
	case int64:
		f = float64(x)
	case string:
		var err error
		if f, err = strconv.ParseFloat(x, 64); err != nil {
			return 0, false
		}
	default:
		return 0, false
	}
	return f, !math.IsNaN(f) && !math.IsInf(f, 0)
}

// NormalizeResource extracts trace-level metadata from resource attributes.
func NormalizeResource(res map[string]any) model.Resource {
	r := newReader(res)
	out := model.Resource{
		ServiceName:  r.str("service.name"),
		Environment:  r.str("agenttwin.environment", "deployment.environment.name", "deployment.environment"),
		ProjectHint:  r.str("agenttwin.project"),
		AgentName:    r.str("agenttwin.agent.name", "gen_ai.agent.name"),
		AgentVersion: r.str("agenttwin.agent.version", "service.version"),
		ReleaseID:    r.str("agenttwin.release.id"),
		CommitSHA:    r.str("agenttwin.commit.sha", "vcs.ref.head.revision"),
		SDKName:      r.str("agenttwin.sdk.name"),
		SDKVersion:   r.str("agenttwin.sdk.version"),
		Source:       r.str("agenttwin.source"),
		ContentMode:  r.str("agenttwin.content.mode"),
	}
	if b := r.bool("agenttwin.content.redacted"); b != nil {
		out.ContentRedacted = *b
	}
	if out.SDKName == "" {
		if lang := r.str("telemetry.sdk.language"); lang != "" {
			out.SDKName = "otel-" + lang
			out.SDKVersion = r.str("telemetry.sdk.version")
		}
	}
	return out
}

// Normalize converts a decoded OTLP span into the internal schema. Content
// candidates are placed in Span.Content; package content decides whether they
// may be stored.
func Normalize(sp otlp.Span) model.Span {
	out := model.Span{
		TraceID: sp.TraceID, SpanID: sp.SpanID, ParentSpanID: sp.ParentSpanID, Name: sp.Name,
		OTelKind: sp.Kind, StatusMessage: sp.StatusMessage, Start: sp.Start, End: sp.End,
		Resource: NormalizeResource(sp.Resource), Truncated: sp.Truncated, SemconvVersion: None,
	}
	switch sp.StatusCode {
	case 1:
		out.Status = model.StatusOK
	case 2:
		out.Status = model.StatusError
	default:
		out.Status = model.StatusUnset
	}
	adapter := Detect(sp.Attrs, sp.Events)
	if adapter != nil {
		out.SemconvVersion = adapter.Version
	}
	r := newReader(sp.Attrs)
	a := &out.Attrs
	content := &model.Content{}

	// Version-specific fields.
	if adapter != nil {
		a.Provider = r.str(adapter.Provider...)
		a.InputTokens = r.count(adapter.InputTokens...)
		a.OutputTokens = r.count(adapter.OutputTokens...)
		if len(adapter.Session) > 0 {
			a.SessionID = r.str(adapter.Session...)
		}
		content.Input = r.str(adapter.InputContent...)
		content.Output = r.str(adapter.OutputContent...)
		if len(adapter.SystemContent) > 0 {
			content.SystemInstructions = r.str(adapter.SystemContent...)
		}
		if content.Input == "" {
			content.Input = joinIndexed(r, adapter.InputContentPrefixes)
		}
		if content.Output == "" {
			content.Output = joinIndexed(r, adapter.OutputContentPrefixes)
		}
	}

	// Attributes shared by all generations plus the agenttwin.* namespace.
	a.Operation = r.str("gen_ai.operation.name")
	a.AgentName = r.str("agenttwin.agent.name", "gen_ai.agent.name")
	a.AgentVersion = r.str("agenttwin.agent.version")
	a.AgentID = r.str("agenttwin.agent.id", "gen_ai.agent.id")
	a.Environment = r.str("agenttwin.environment", "deployment.environment.name", "deployment.environment")
	a.Source = strings.ToLower(r.str("agenttwin.source"))
	a.ReleaseID = r.str("agenttwin.release.id")
	a.CommitSHA = r.str("agenttwin.commit.sha", "vcs.ref.head.revision")
	if a.SessionID == "" {
		a.SessionID = r.str("agenttwin.session.id", "session.id")
	}
	a.RequestModel = r.str("gen_ai.request.model")
	a.ResponseModel = r.str("gen_ai.response.model")
	a.Temperature = r.float("gen_ai.request.temperature")
	a.MaxTokens = r.count("gen_ai.request.max_tokens")
	a.FinishReasons = r.strings("gen_ai.response.finish_reasons")
	a.CostUSD = r.amount("agenttwin.cost.usd")
	a.PromptHash = r.str("agenttwin.prompt.hash")
	a.PromptVersion = r.str("agenttwin.prompt.version")
	a.ToolName = r.str("gen_ai.tool.name", "agenttwin.tool.name")
	a.ToolCallID = r.str("gen_ai.tool.call.id")
	a.ToolVersion = r.str("agenttwin.tool.version")
	a.ToolRisk = strings.ToUpper(r.str("agenttwin.tool.risk"))
	a.ToolArgsHash = r.str("agenttwin.tool.args_hash")
	a.ToolResultStatus = strings.ToLower(r.str("agenttwin.tool.result_status"))
	a.IdempotencyKey = r.str("agenttwin.tool.idempotency_key_hash")
	a.Attempt = r.count("agenttwin.tool.attempt")
	a.RetrievalSource = r.str("agenttwin.retrieval.source")
	a.DocumentCount = r.count("agenttwin.retrieval.document_count")
	a.ErrorType = r.str("error.type")
	a.PolicyDecision = strings.ToLower(r.str("agenttwin.policy.decision"))
	a.PolicyName = r.str("agenttwin.policy.name")
	a.PolicyVersion = r.str("agenttwin.policy.version")
	a.PolicyRule = r.str("agenttwin.policy.rule")
	a.OutcomeStatus = strings.ToUpper(r.str("agenttwin.outcome.status"))
	a.OutcomeClaimed = strings.ToUpper(r.str("agenttwin.outcome.claimed"))
	a.BusinessOutcome = r.str("agenttwin.outcome.business")
	a.OutcomeVerified = r.bool("agenttwin.outcome.verified")
	a.VerificationSource = strings.ToLower(r.str("agenttwin.outcome.verification_source"))
	a.HTTPMethod = r.str("http.request.method", "http.method")
	a.HTTPStatus = r.int("http.response.status_code", "http.status_code")
	a.HTTPHost = r.str("server.address", "net.peer.name", "http.host")
	if a.HTTPHost == "" {
		if u := r.str("url.full", "http.url"); u != "" {
			if parsed, err := url.Parse(u); err == nil {
				a.HTTPHost = parsed.Hostname()
			}
		}
	}
	a.MCPMethod = r.str("mcp.method.name")
	a.MCPServer = r.str("agenttwin.mcp.server", "mcp.server.name")
	a.SimulationRunID = r.str("agenttwin.simulation.run_id")
	a.ScenarioID = r.str("agenttwin.scenario.id")
	if v, ok := r.get("agenttwin.state.diff"); ok {
		a.FinalStateDiff = asObject(v)
	}
	// Content that is identical across generations.
	if content.ToolArgs == "" {
		content.ToolArgs = r.str("agenttwin.tool.args", "gen_ai.tool.call.arguments")
	}
	if content.ToolResult == "" {
		content.ToolResult = r.str("agenttwin.tool.result", "gen_ai.tool.call.result")
	}
	if content.Input == "" {
		content.Input = r.str("agenttwin.input")
	}
	if content.Output == "" {
		content.Output = r.str("agenttwin.output")
	}
	redacted := r.bool("agenttwin.content.redacted")
	if redacted != nil && *redacted {
		out.Resource.ContentRedacted = true
	}
	explicitKind := model.Kind(strings.ToLower(r.str("agenttwin.span.kind")))

	// Events: exceptions become error facts, legacy content events become
	// content, the rest keep only scalar attributes.
	for _, ev := range sp.Events {
		switch {
		case ev.Name == "exception":
			er := newReader(ev.Attrs)
			if a.ExceptionType == "" {
				a.ExceptionType = er.str("exception.type")
				a.ExceptionMessage = er.str("exception.message")
			}
			out.Events = append(out.Events, model.Event{Name: ev.Name, Time: ev.Time,
				Attrs: map[string]any{"exception.type": er.str("exception.type")}})
		case adapter != nil && adapter.ContentEvents[ev.Name].attr != "":
			ce := adapter.ContentEvents[ev.Name]
			text := toString(ev.Attrs[ce.attr])
			if ce.input && content.Input == "" {
				content.Input = text
			} else if !ce.input && content.Output == "" {
				content.Output = text
			}
		default:
			attrs := map[string]any{}
			for k, v := range ev.Attrs {
				if IsContentKey(k) {
					continue
				}
				if s, ok := scalar(v); ok {
					attrs[k] = s
				}
			}
			out.Events = append(out.Events, model.Event{Name: ev.Name, Time: ev.Time, Attrs: attrs})
		}
	}

	out.Kind = inferKind(explicitKind, out, sp)
	clipIdentifiers(&out)
	if !content.Empty() {
		out.Content = content
	}

	// Remaining attributes: never content, only scalars (sanitized later).
	extra := map[string]any{}
	keys := make([]string, 0, len(sp.Attrs))
	for k := range sp.Attrs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if r.used[k] || IsContentKey(k) {
			continue
		}
		if s, ok := scalar(sp.Attrs[k]); ok {
			extra[k] = s
		}
	}
	if len(extra) > 0 {
		a.Extra = extra
	}
	return out
}

// Identifier-like values end up in indexed columns and array indexes, whose
// entries have hard size limits: one oversized attribute must not fail a
// whole ingest batch. Content is bounded separately by package content.
const (
	maxIdentifierBytes = 256
	maxMessageBytes    = 2048
	maxFinishReasons   = 16
)

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}

func clipIdentifiers(sp *model.Span) {
	a := &sp.Attrs
	for _, f := range []*string{&a.Operation, &a.AgentName, &a.AgentVersion, &a.AgentID, &a.SessionID, &a.Environment,
		&a.Source, &a.ReleaseID, &a.CommitSHA, &a.Provider, &a.RequestModel, &a.ResponseModel, &a.PromptHash, &a.PromptVersion,
		&a.ToolName, &a.ToolVersion, &a.ToolCallID, &a.ToolRisk, &a.ToolArgsHash, &a.ToolResultStatus, &a.IdempotencyKey,
		&a.RetrievalSource, &a.ErrorType, &a.ExceptionType, &a.PolicyDecision, &a.PolicyName, &a.PolicyVersion, &a.PolicyRule,
		&a.OutcomeStatus, &a.OutcomeClaimed, &a.BusinessOutcome, &a.VerificationSource, &a.HTTPMethod, &a.HTTPHost,
		&a.MCPMethod, &a.MCPServer, &a.SimulationRunID, &a.ScenarioID} {
		*f = clip(*f, maxIdentifierBytes)
	}
	if len(a.FinishReasons) > maxFinishReasons {
		a.FinishReasons = a.FinishReasons[:maxFinishReasons]
	}
	for i := range a.FinishReasons {
		a.FinishReasons[i] = clip(a.FinishReasons[i], 64)
	}
	a.ExceptionMessage = clip(a.ExceptionMessage, maxMessageBytes)
	sp.StatusMessage = clip(sp.StatusMessage, maxMessageBytes)
	r := &sp.Resource
	for _, f := range []*string{&r.ServiceName, &r.Environment, &r.ProjectHint, &r.AgentName, &r.AgentVersion, &r.ReleaseID,
		&r.CommitSHA, &r.SDKName, &r.SDKVersion, &r.Source, &r.ContentMode} {
		*f = clip(*f, maxIdentifierBytes)
	}
}

// joinIndexed concatenates an indexed attribute family such as
// gen_ai.prompt.0.role / gen_ai.prompt.0.content in index order.
func joinIndexed(r *attrReader, prefixes []string) string {
	if len(prefixes) == 0 {
		return ""
	}
	type part struct {
		idx  int
		role string
		text string
	}
	parts := map[int]*part{}
	for k, v := range r.m {
		for _, p := range prefixes {
			if !strings.HasPrefix(k, p) {
				continue
			}
			rest := strings.TrimPrefix(k, p)
			segs := strings.SplitN(rest, ".", 2)
			if len(segs) != 2 {
				continue
			}
			i, err := strconv.Atoi(segs[0])
			if err != nil {
				continue
			}
			r.used[k] = true
			pt := parts[i]
			if pt == nil {
				pt = &part{idx: i}
				parts[i] = pt
			}
			switch segs[1] {
			case "role":
				pt.role = toString(v)
			case "content":
				pt.text = toString(v)
			}
		}
	}
	if len(parts) == 0 {
		return ""
	}
	idx := make([]int, 0, len(parts))
	for i := range parts {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	var b strings.Builder
	for _, i := range idx {
		p := parts[i]
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		if p.role != "" {
			fmt.Fprintf(&b, "%s: ", p.role)
		}
		b.WriteString(p.text)
	}
	return b.String()
}

func inferKind(explicit model.Kind, s model.Span, raw otlp.Span) model.Kind {
	if model.ValidKind(explicit) {
		return explicit
	}
	a := s.Attrs
	switch strings.ToLower(a.Operation) {
	case "chat", "text_completion", "generate_content", "embeddings":
		return model.KindModel
	case "execute_tool":
		return model.KindTool
	case "invoke_agent", "create_agent":
		return model.KindAgent
	}
	name := strings.ToLower(s.Name)
	switch {
	case a.PolicyDecision != "":
		return model.KindPolicy
	case a.OutcomeStatus != "":
		return model.KindOutcome
	case a.ToolName != "":
		return model.KindTool
	case a.MCPMethod != "" || strings.HasPrefix(name, "mcp."):
		return model.KindMCP
	case a.Provider != "" || a.RequestModel != "" || a.InputTokens != nil:
		return model.KindModel
	case strings.Contains(name, "retriev") || a.RetrievalSource != "":
		return model.KindRetrieval
	case a.HTTPMethod != "":
		return model.KindHTTP
	case strings.HasPrefix(name, "agent.") || strings.HasPrefix(name, "invoke_agent"):
		return model.KindAgent
	case raw.ParentSpanID == "":
		return model.KindAgent
	}
	return model.KindOther
}

// scalar keeps strings, numbers and booleans (arrays of scalars joined).
func scalar(v any) (any, bool) {
	switch x := v.(type) {
	case string, bool, int64, float64:
		return x, true
	case []any:
		parts := make([]string, 0, len(x))
		for _, e := range x {
			if s, ok := scalar(e); ok {
				parts = append(parts, toString(s))
			}
		}
		return strings.Join(parts, ","), true
	}
	return nil, false
}

func asObject(v any) map[string]any {
	switch x := v.(type) {
	case map[string]any:
		return x
	case string:
		var m map[string]any
		if json.Unmarshal([]byte(x), &m) == nil {
			return m
		}
	}
	return nil
}
