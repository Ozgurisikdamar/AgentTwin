package graph

import (
	"context"
	"math/rand/v2"
	"slices"
)

// memory is an in-memory Reader for tests. With shuffle set it answers in a
// random order, as a database without ORDER BY may.
type memory struct {
	nodes   map[string]Node
	edges   []Edge
	shuffle *rand.Rand
	calls   int
}

func newMemory() *memory { return &memory{nodes: map[string]Node{}} }

func (m *memory) node(kind Kind, key string, attrs map[string]any) string {
	id := string(kind) + ":" + key
	if attrs == nil {
		attrs = map[string]any{}
	}
	m.nodes[id] = Node{ID: id, Kind: kind, Key: key, Label: key, Attrs: attrs}
	return id
}

func (m *memory) edge(from string, t EdgeType, to string, sources ...Source) {
	if len(sources) == 0 {
		sources = []Source{SourceManifest}
	}
	// Like the store: one edge per (from, type, to), its evidence combined.
	id := from + "-" + string(t) + "-" + to
	for i, e := range m.edges {
		if e.ID == id {
			for _, s := range sources {
				if !slices.Contains(e.Sources, s) {
					e.Sources = append(e.Sources, s)
				}
			}
			slices.Sort(e.Sources)
			e.Confidence = CombinedConfidence(e.Sources)
			m.edges[i] = e
			return
		}
	}
	m.edges = append(m.edges, Edge{ID: id, From: from, To: to, Type: t,
		Confidence: CombinedConfidence(sources), Sources: sources})
}

func (m *memory) Lookup(_ context.Context, refs []Ref) (map[Ref]Node, error) {
	m.calls++
	out := map[Ref]Node{}
	for _, r := range refs {
		if n, ok := m.nodes[string(r.Kind)+":"+r.Key]; ok {
			out[r] = n
		}
	}
	return out, nil
}

func (m *memory) Nodes(_ context.Context, ids []string) (map[string]Node, error) {
	m.calls++
	out := map[string]Node{}
	for _, id := range ids {
		if n, ok := m.nodes[id]; ok {
			out[id] = n
		}
	}
	return out, nil
}

func (m *memory) Edges(_ context.Context, ids []string, out bool, types []EdgeType) ([]Edge, error) {
	m.calls++
	var res []Edge
	for _, e := range m.edges {
		end := e.To
		if out {
			end = e.From
		}
		if slices.Contains(ids, end) && slices.Contains(types, e.Type) {
			res = append(res, e)
		}
	}
	if m.shuffle != nil {
		m.shuffle.Shuffle(len(res), func(i, j int) { res[i], res[j] = res[j], res[i] })
	}
	return res, nil
}

// demoGraph is Demo Co's support-refund agent as its manifests, OpenAPI
// import and scenarios describe it: versions 1.2.4 and 1.3.0 (the latest)
// differ only in their prompt.
func demoGraph() *memory {
	m := newMemory()
	agent := m.node(KindAgent, "support-refund-agent", nil)
	v124 := m.node(KindAgentVersion, "support-refund-agent@1.2.4", map[string]any{"agent": "support-refund-agent", "version": "1.2.4", "latest": false})
	v130 := m.node(KindAgentVersion, "support-refund-agent@1.3.0", map[string]any{"agent": "support-refund-agent", "version": "1.3.0", "latest": true})
	p1 := m.node(KindPrompt, "sha256:aaaa", nil)
	p2 := m.node(KindPrompt, "sha256:bbbb", nil)
	model := m.node(KindModel, "scripted/scripted-planner-v1", nil)
	kb := m.node(KindRetrieval, "support-kb", nil)
	risks := map[string]string{
		"lookup_customer": "READ", "lookup_order": "READ", "get_refund_policy": "READ",
		"refund_payment": "WRITE_IRREVERSIBLE", "send_email": "WRITE_REVERSIBLE",
		"escalate_to_human": "WRITE_REVERSIBLE", "export_customer_data": "ADMIN",
	}
	tools := map[string]string{}
	for name, risk := range risks {
		tools[name] = m.node(KindTool, name, map[string]any{"risk": risk})
	}
	for _, v := range []string{v124, v130} {
		m.edge(v, EdgeVersionOf, agent)
		m.edge(v, EdgeUses, model)
		m.edge(v, EdgeRetrievesFrom, kb)
		for _, t := range tools {
			m.edge(v, EdgeUses, t)
		}
	}
	m.edge(v124, EdgeUses, p1)
	m.edge(v130, EdgeUses, p2)
	payments := m.node(KindService, "payments-api", map[string]any{"criticality": "CRITICAL"})
	paymentsDB := m.node(KindDatabase, "payments-db", map[string]any{"criticality": "CRITICAL"})
	orders := m.node(KindService, "orders-api", map[string]any{"criticality": "HIGH"})
	email := m.node(KindExternal, "email-provider", map[string]any{"criticality": "MEDIUM"})
	api := m.node(KindHTTPAPI, "payments-api", nil)
	m.edge(tools["refund_payment"], EdgeCalls, payments)
	m.edge(tools["refund_payment"], EdgeWrites, paymentsDB)
	m.edge(tools["refund_payment"], EdgeCanMutate, api, SourceOpenAPI)
	m.edge(api, EdgeDependsOn, payments, SourceOpenAPI)
	m.edge(tools["lookup_order"], EdgeCalls, orders)
	m.edge(tools["lookup_order"], EdgeReads, m.node(KindDatabase, "orders-db", map[string]any{"criticality": "CRITICAL"}))
	m.edge(tools["send_email"], EdgeCalls, email)
	policy := m.node(KindPolicy, "refund-limit", nil)
	m.edge(tools["refund_payment"], EdgeGuardedBy, policy)
	scenarios := map[string][2]any{
		"refund-happy-path":             {"high", []string{"lookup_order", "get_refund_policy", "refund_payment", "send_email"}},
		"refund-timeout-after-mutation": {"critical", []string{"refund_payment"}},
		"refund-tool-success-lie":       {"critical", []string{"refund_payment"}},
		"refund-rate-limited":           {"high", []string{"refund_payment"}},
		"unauthorized-admin-tool":       {"critical", []string{"export_customer_data"}},
		"malicious-retrieved-content":   {"critical", []string{}},
		"refund-policy-lookup":          {"medium", []string{"get_refund_policy"}},
	}
	for name, spec := range scenarios {
		s := m.node(KindScenario, name, map[string]any{"severity": spec[0]})
		m.edge(agent, EdgeTestedBy, s, SourceScenario)
		for _, t := range spec[1].([]string) {
			m.edge(tools[t], EdgeTestedBy, s, SourceScenario)
		}
		if name == "malicious-retrieved-content" {
			m.edge(kb, EdgeTestedBy, s, SourceScenario)
		}
	}
	return m
}
