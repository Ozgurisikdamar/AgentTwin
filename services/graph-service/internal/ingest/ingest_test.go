package ingest

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/graph"
)

var at = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

func ref(kind graph.Kind, key string) graph.Ref { return graph.Ref{Kind: kind, Key: key} }

// edge finds the fact for from -type-> to, failing when it is absent.
func edge(t *testing.T, f Facts, from graph.Ref, typ graph.EdgeType, to graph.Ref) EdgeFact {
	t.Helper()
	for _, e := range f.Edges {
		if e.From == from && e.Type == typ && e.To == to {
			return e
		}
	}
	t.Fatalf("no edge %s -%s-> %s in %+v", from, typ, to, f.Edges)
	return EdgeFact{}
}

func node(t *testing.T, f Facts, r graph.Ref) NodeFact {
	t.Helper()
	for _, n := range f.Nodes {
		if n.Ref == r {
			return n
		}
	}
	t.Fatalf("no node %s", r)
	return NodeFact{}
}

func raw(t *testing.T, v any) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// agentPayload is what the control plane emits for demo manifest 1.3.0.
func agentPayload() map[string]any {
	return map[string]any{
		"agent_id": "0192f0c0-0000-7000-8000-000000000001", "agent_name": "support-refund-agent",
		"version_id": "0192f0c0-0000-7000-8000-000000000002", "version": "1.3.0",
		"manifest_hash": strings.Repeat("a", 64), "prompt_hash": "sha256:" + strings.Repeat("b", 64),
		"model": map[string]any{"provider": "scripted", "name": "scripted-planner-v1"},
		"tools": []map[string]any{
			{"name": "refund_payment", "risk": "WRITE_IRREVERSIBLE"}, {"name": "lookup_order", "risk": "READ"},
		},
		"retrieval_sources": []string{"support-kb"},
		"dependencies": []map[string]any{
			{"tool": "refund_payment", "kind": "SERVICE", "name": "payments-api", "relation": "CALLS", "criticality": "CRITICAL"},
			{"tool": "refund_payment", "kind": "DATABASE", "name": "payments-db", "relation": "WRITES", "criticality": "CRITICAL"},
			{"tool": "lookup_order", "kind": "service", "name": "orders-api", "relation": "", "criticality": ""},
		},
	}
}

func TestAgentVersionIsDeclaredByItsManifest(t *testing.T) {
	f, err := FromAgentVersion(raw(t, agentPayload()), at)
	if err != nil {
		t.Fatal(err)
	}
	agent := ref(graph.KindAgent, "support-refund-agent")
	version := ref(graph.KindAgentVersion, "support-refund-agent@1.3.0")
	v := node(t, f, version)
	for k, want := range map[string]any{
		"agent": "support-refund-agent", "version": "1.3.0", "version_id": "0192f0c0-0000-7000-8000-000000000002",
		"manifest_hash": strings.Repeat("a", 64), "registered_at": "2026-09-01T12:00:00Z",
	} {
		if v.Attrs[k] != want {
			t.Errorf("version attribute %s = %v, want %v", k, v.Attrs[k], want)
		}
	}
	const src = "manifest:support-refund-agent@1.3.0"
	for _, e := range []EdgeFact{
		edge(t, f, version, graph.EdgeVersionOf, agent),
		edge(t, f, version, graph.EdgeUses, ref(graph.KindPrompt, "sha256:"+strings.Repeat("b", 64))),
		edge(t, f, version, graph.EdgeUses, ref(graph.KindModel, "scripted/scripted-planner-v1")),
		edge(t, f, version, graph.EdgeUses, ref(graph.KindTool, "refund_payment")),
		edge(t, f, version, graph.EdgeRetrievesFrom, ref(graph.KindRetrieval, "support-kb")),
		edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgeCalls, ref(graph.KindService, "payments-api")),
		edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgeWrites, ref(graph.KindDatabase, "payments-db")),
	} {
		if e.Source != graph.SourceManifest || e.SourceRef != src {
			t.Errorf("%s -%s-> %s evidence = %s %s, want MANIFEST %s", e.From, e.Type, e.To, e.Source, e.SourceRef, src)
		}
	}
	if got := node(t, f, ref(graph.KindTool, "refund_payment")).Attrs["risk"]; got != "WRITE_IRREVERSIBLE" {
		t.Errorf("tool risk = %v", got)
	}
	if got := node(t, f, ref(graph.KindPrompt, "sha256:"+strings.Repeat("b", 64))).Label; got != "prompt bbbbbbbbbbbb" {
		t.Errorf("prompt label = %q", got)
	}
	if got := node(t, f, ref(graph.KindDatabase, "payments-db")).Attrs["criticality"]; got != "CRITICAL" {
		t.Errorf("dependency criticality = %v", got)
	}
	// A dependency without a relation depends on its system; kinds are
	// case-insensitive; no criticality means none is claimed.
	edge(t, f, ref(graph.KindTool, "lookup_order"), graph.EdgeDependsOn, ref(graph.KindService, "orders-api"))
	if _, claimed := node(t, f, ref(graph.KindService, "orders-api")).Attrs["criticality"]; claimed {
		t.Error("a dependency without criticality was given one")
	}
	if !slices.Equal(f.LatestOf, []string{"support-refund-agent"}) {
		t.Errorf("latest recomputed for %v", f.LatestOf)
	}
	if len(f.Replaces) != 0 {
		t.Errorf("a manifest version replaces nothing (versions coexist), got %v", f.Replaces)
	}
}

func TestAgentVersionWithoutOptionalParts(t *testing.T) {
	p := agentPayload()
	p["prompt_hash"], p["model"], p["retrieval_sources"], p["dependencies"] = nil, nil, nil, nil
	f, err := FromAgentVersion(raw(t, p), at)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range f.Edges {
		if e.To.Kind == graph.KindPrompt || e.To.Kind == graph.KindModel || e.To.Kind == graph.KindRetrieval {
			t.Errorf("unexpected edge to %s", e.To)
		}
	}
	if len(f.Edges) != 3 { // VERSION_OF + two tools
		t.Errorf("%d edges, want 3", len(f.Edges))
	}
}

func TestAgentVersionRejectsWhatCannotBeGraphed(t *testing.T) {
	for name, mutate := range map[string]func(map[string]any){
		"no agent name": func(p map[string]any) { p["agent_name"] = "" },
		"no version":    func(p map[string]any) { p["version"] = "" },
		"unknown dependency kind": func(p map[string]any) {
			p["dependencies"] = []map[string]any{{"tool": "refund_payment", "kind": "SCENARIO", "name": "x"}}
		},
		"unknown relation": func(p map[string]any) {
			p["dependencies"] = []map[string]any{{"tool": "refund_payment", "kind": "SERVICE", "name": "x", "relation": "TESTED_BY"}}
		},
		"unknown criticality": func(p map[string]any) {
			p["dependencies"] = []map[string]any{{"tool": "refund_payment", "kind": "SERVICE", "name": "x", "criticality": "EXTREME"}}
		},
		"unnamed tool": func(p map[string]any) { p["tools"] = []map[string]any{{"name": "", "risk": "READ"}} },
	} {
		t.Run(name, func(t *testing.T) {
			p := agentPayload()
			mutate(p)
			if _, err := FromAgentVersion(raw(t, p), at); err == nil {
				t.Error("accepted")
			}
		})
	}
	if _, err := FromAgentVersion(json.RawMessage(`[1]`), at); err == nil {
		t.Error("accepted a payload that is not an object")
	}
}

func TestOpenAPIImportDescribesAnHTTPAPI(t *testing.T) {
	f, err := FromToolCatalog(raw(t, map[string]any{
		"source": "OPENAPI", "source_name": "payments-api", "service": "payments",
		"tools": []map[string]any{
			{"name": "refund_payment", "risk": "WRITE_IRREVERSIBLE", "method": "POST", "path": "/refunds", "mutating": true},
			{"name": "get_refund", "risk": "READ", "method": "GET", "path": "/refunds/{id}", "mutating": false},
		},
	}))
	if err != nil {
		t.Fatal(err)
	}
	api := ref(graph.KindHTTPAPI, "payments-api")
	mut := edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgeCanMutate, api)
	if mut.Source != graph.SourceOpenAPI || mut.SourceRef != "openapi:payments-api" ||
		mut.Detail["method"] != "POST" || mut.Detail["path"] != "/refunds" || mut.Detail["mutating"] != true {
		t.Errorf("mutating operation = %+v", mut)
	}
	read := edge(t, f, ref(graph.KindTool, "get_refund"), graph.EdgeCalls, api)
	if read.Detail["mutating"] != false {
		t.Errorf("read operation = %+v", read)
	}
	edge(t, f, api, graph.EdgeDependsOn, ref(graph.KindService, "payments"))
	// An import describes an interface: its risk does not overwrite a
	// manifest's declaration.
	tool := node(t, f, ref(graph.KindTool, "refund_payment"))
	if _, set := tool.Attrs["risk"]; set || tool.Attrs["imported_risk"] != "WRITE_IRREVERSIBLE" {
		t.Errorf("imported tool attributes = %v", tool.Attrs)
	}
	if !slices.Equal(f.Replaces, []Replace{{graph.SourceOpenAPI, "openapi:payments-api"}}) {
		t.Errorf("replaces = %v", f.Replaces)
	}
}

func TestMCPImportAndManualCatalog(t *testing.T) {
	f, err := FromToolCatalog(raw(t, map[string]any{
		"source": "MCP", "source_name": "crm", "tools": []map[string]any{{"name": "update_ticket", "risk": "WRITE_REVERSIBLE", "mutating": true}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	e := edge(t, f, ref(graph.KindTool, "update_ticket"), graph.EdgeCalls, ref(graph.KindMCPServer, "crm"))
	if e.Source != graph.SourceMCP || e.SourceRef != "mcp:crm" || e.Source.Confidence() >= graph.SourceOpenAPI.Confidence() {
		t.Errorf("mcp evidence = %+v", e)
	}
	if !slices.Equal(f.Replaces, []Replace{{graph.SourceMCP, "mcp:crm"}}) {
		t.Errorf("replaces = %v", f.Replaces)
	}

	f, err = FromToolCatalog(raw(t, map[string]any{
		"source": "MANUAL", "source_name": "hand", "tools": []map[string]any{{"name": "lookup_order", "risk": "READ"}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Edges) != 0 || len(f.Replaces) != 0 || node(t, f, ref(graph.KindTool, "lookup_order")).Attrs["risk"] != "READ" {
		t.Errorf("manual catalog = %+v", f)
	}
	if _, err := FromToolCatalog(raw(t, map[string]any{"source": "GRAPHQL", "source_name": "x", "tools": []any{}})); err == nil {
		t.Error("unknown source accepted")
	}
}

func tracePayload(source string) map[string]any {
	return map[string]any{
		"trace_id": strings.Repeat("c", 32), "agent": "support-refund-agent", "agent_version": "1.3.0",
		"environment": "production", "source": source, "summary": map[string]any{}, "signals": []string{},
		"observed_tools": []map[string]any{
			{"name": "refund_payment", "risk": "WRITE_IRREVERSIBLE", "http_host": "payments.internal", "count": 2},
			{"name": "lookup_order", "count": 0},
			{"name": ""},
		},
	}
}

func TestProductionTracesAreObservedUsage(t *testing.T) {
	for _, source := range []string{"production", ""} { // older traces carry no source
		f, err := FromTrace(raw(t, tracePayload(source)))
		if err != nil {
			t.Fatal(err)
		}
		version := ref(graph.KindAgentVersion, "support-refund-agent@1.3.0")
		uses := edge(t, f, version, graph.EdgeUses, ref(graph.KindTool, "refund_payment"))
		if uses.Source != graph.SourceObserved || uses.SourceRef != "traces" || uses.Observations != 2 ||
			uses.Detail["last_trace_id"] != strings.Repeat("c", 32) {
			t.Errorf("observed use = %+v", uses)
		}
		if n := edge(t, f, version, graph.EdgeUses, ref(graph.KindTool, "lookup_order")).Observations; n != 1 {
			t.Errorf("a call without a count counts once, got %d", n)
		}
		host := edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgeCalls, ref(graph.KindHTTPAPI, "payments.internal"))
		if host.Observations != 2 {
			t.Errorf("host calls = %d", host.Observations)
		}
		edge(t, f, version, graph.EdgeVersionOf, ref(graph.KindAgent, "support-refund-agent"))
		if len(f.Edges) != 4 {
			t.Errorf("%d edges, want 4 (an unnamed tool is skipped)", len(f.Edges))
		}
		if len(f.LatestOf) != 0 || len(f.Replaces) != 0 {
			t.Error("a trace neither registers a version nor replaces evidence")
		}
	}
}

func TestNonProductionTracesAreNotUsage(t *testing.T) {
	for _, source := range []string{"simulation", "replay", "eval", "test"} {
		f, err := FromTrace(raw(t, tracePayload(source)))
		if err != nil || len(f.Nodes) != 0 || len(f.Edges) != 0 {
			t.Errorf("%s trace mapped to %+v (%v)", source, f, err)
		}
	}
	p := tracePayload("production")
	p["agent_version"] = nil
	f, err := FromTrace(raw(t, p))
	if err != nil {
		t.Fatal(err)
	}
	edge(t, f, ref(graph.KindAgent, "support-refund-agent"), graph.EdgeUses, ref(graph.KindTool, "refund_payment"))
}

func TestScenarioCoversWhatItNames(t *testing.T) {
	agent := "support-refund-agent"
	f, err := FromScenario(raw(t, map[string]any{
		"scenario_id": "s1", "scenario_version_id": "sv1", "name": "refund-happy-path", "agent": agent,
		"severity": "high", "spec_hash": "h",
		"covers": []string{"tool:refund_payment", "retrieval:support-kb", "policy:refund-limit", "fault:rate_limit", "tool:", "threat:x"},
	}))
	if err != nil {
		t.Fatal(err)
	}
	s := ref(graph.KindScenario, "refund-happy-path")
	n := node(t, f, s)
	if n.Attrs["severity"] != "high" || !slices.Equal(n.Attrs["covers_other"].([]string), []string{"fault:rate_limit", "tool:", "threat:x"}) ||
		len(n.Attrs["tags"].([]string)) != 0 {
		t.Errorf("scenario attributes = %v", n.Attrs)
	}
	for _, from := range []graph.Ref{
		ref(graph.KindAgent, agent), ref(graph.KindTool, "refund_payment"),
		ref(graph.KindRetrieval, "support-kb"), ref(graph.KindPolicy, "refund-limit"),
	} {
		e := edge(t, f, from, graph.EdgeTestedBy, s)
		if e.Source != graph.SourceScenario || e.SourceRef != "scenario:refund-happy-path" {
			t.Errorf("%s evidence = %s %s", from, e.Source, e.SourceRef)
		}
	}
	if len(f.Edges) != 4 {
		t.Errorf("%d edges, want 4", len(f.Edges))
	}
	if !slices.Equal(f.Replaces, []Replace{{graph.SourceScenario, "scenario:refund-happy-path"}}) {
		t.Errorf("replaces = %v", f.Replaces)
	}
	if _, err := FromScenario(raw(t, map[string]any{"name": "", "covers": []string{}})); err == nil {
		t.Error("unnamed scenario accepted")
	}
}

func TestPolicyGuardsItsTarget(t *testing.T) {
	f, err := FromPolicy(raw(t, map[string]any{"policy_id": "p1", "name": "refund-limit", "version": 2, "target_tool": "refund_payment", "spec_hash": "h"}))
	if err != nil {
		t.Fatal(err)
	}
	e := edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgeGuardedBy, ref(graph.KindPolicy, "refund-limit"))
	if e.Source != graph.SourceManual || e.SourceRef != "policy:refund-limit" {
		t.Errorf("policy evidence = %+v", e)
	}
	if !slices.Equal(f.Replaces, []Replace{{graph.SourceManual, "policy:refund-limit"}}) {
		t.Errorf("replaces = %v", f.Replaces)
	}
	if _, err := FromPolicy(raw(t, map[string]any{"name": "refund-limit"})); err == nil {
		t.Error("policy without target accepted")
	}
}

func TestMappingReplacesTheToolsPreviousMapping(t *testing.T) {
	var m Mapping
	if err := json.Unmarshal([]byte(`{"depends_on":[{"kind":"QUEUE","name":"refunds","relation":"PUBLISHES","criticality":"high"}]}`), &m); err != nil {
		t.Fatal(err)
	}
	f, err := FromMapping("refund_payment", m)
	if err != nil {
		t.Fatal(err)
	}
	e := edge(t, f, ref(graph.KindTool, "refund_payment"), graph.EdgePublishes, ref(graph.KindQueue, "refunds"))
	if e.Source != graph.SourceManual || e.SourceRef != "mapping:refund_payment" {
		t.Errorf("mapping evidence = %+v", e)
	}
	if node(t, f, ref(graph.KindQueue, "refunds")).Attrs["criticality"] != "HIGH" {
		t.Error("criticality not normalized")
	}
	// An empty mapping still replaces: it is how a mapping is removed.
	f, err = FromMapping("refund_payment", Mapping{})
	if err != nil || len(f.Edges) != 0 || !slices.Equal(f.Replaces, []Replace{{graph.SourceManual, "mapping:refund_payment"}}) {
		t.Errorf("empty mapping = %+v (%v)", f, err)
	}
}

func TestValidateRejectsMalformedFacts(t *testing.T) {
	good := ref(graph.KindTool, "t")
	for name, f := range map[string]Facts{
		"node key":  {Nodes: []NodeFact{{Ref: ref(graph.KindTool, "")}}},
		"node kind": {Nodes: []NodeFact{{Ref: ref("GADGET", "t")}}},
		"edge from": {Edges: []EdgeFact{{From: ref(graph.KindTool, "a\x00"), To: good, Type: graph.EdgeCalls, Source: graph.SourceManual, SourceRef: "r"}}},
		"edge to":   {Edges: []EdgeFact{{From: good, To: ref("GADGET", "x"), Type: graph.EdgeCalls, Source: graph.SourceManual, SourceRef: "r"}}},
		"edge type": {Edges: []EdgeFact{{From: good, To: good, Type: "LIKES", Source: graph.SourceManual, SourceRef: "r"}}},
		"source":    {Edges: []EdgeFact{{From: good, To: good, Type: graph.EdgeCalls, Source: "RUMOUR", SourceRef: "r"}}},
		"no ref":    {Edges: []EdgeFact{{From: good, To: good, Type: graph.EdgeCalls, Source: graph.SourceManual}}},
		"long ref":  {Edges: []EdgeFact{{From: good, To: good, Type: graph.EdgeCalls, Source: graph.SourceManual, SourceRef: strings.Repeat("r", 301)}}},
	} {
		if err := f.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	ok := Facts{Edges: []EdgeFact{{From: good, To: good, Type: graph.EdgeCalls, Source: graph.SourceManual, SourceRef: strings.Repeat("r", 300)}}}
	if err := ok.Validate(); err != nil {
		t.Errorf("valid facts rejected: %v", err)
	}
}
