package graph

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"slices"
	"testing"
)

func keys(ns []Node) []string {
	out := make([]string, len(ns))
	for i, n := range ns {
		out[i] = n.Ref().String()
	}
	return out
}

func mustView(t *testing.T, m *memory, focus []string, depth, limit int, kinds []Kind) View {
	t.Helper()
	var f []Node
	for _, id := range focus {
		f = append(f, m.nodes[id])
	}
	v, err := Neighborhood(context.Background(), m, f, depth, limit, kinds)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestNeighborhoodWalksBothDirectionsToTheDepth(t *testing.T) {
	m := demoGraph()
	v := mustView(t, m, []string{"TOOL:refund_payment"}, 1, 0, nil)
	got := keys(v.Nodes)
	// One hop from the tool: the versions that use it, what it calls,
	// writes and mutates, its policy and the scenarios that test it.
	for _, want := range []string{
		"TOOL:refund_payment", "AGENT_VERSION:support-refund-agent@1.2.4", "AGENT_VERSION:support-refund-agent@1.3.0",
		"SERVICE:payments-api", "DATABASE:payments-db", "HTTP_API:payments-api", "POLICY:refund-limit",
		"SCENARIO:refund-happy-path", "SCENARIO:refund-rate-limited",
	} {
		if !slices.Contains(got, want) {
			t.Errorf("depth 1 misses %s", want)
		}
	}
	for _, far := range []string{"AGENT:support-refund-agent", "TOOL:lookup_order", "PROMPT:sha256:aaaa"} {
		if slices.Contains(got, far) {
			t.Errorf("depth 1 reaches %s, two hops away", far)
		}
	}
	if got[0] != "TOOL:refund_payment" || v.Depth != 1 || v.Truncated {
		t.Errorf("focus first, depth 1, not truncated: %v %d %v", got[0], v.Depth, v.Truncated)
	}
	v2 := mustView(t, m, []string{"TOOL:refund_payment"}, 2, 0, nil)
	if !slices.Contains(keys(v2.Nodes), "AGENT:support-refund-agent") || !slices.Contains(keys(v2.Nodes), "TOOL:lookup_order") {
		t.Error("depth 2 reaches the agent and its other tools")
	}
}

func TestNeighborhoodEdgesJoinShownComponentsOnly(t *testing.T) {
	m := demoGraph()
	v := mustView(t, m, []string{"TOOL:refund_payment"}, 1, 0, nil)
	shown := map[string]bool{}
	for _, n := range v.Nodes {
		shown[n.ID] = true
	}
	if len(v.Edges) == 0 {
		t.Fatal("no edges")
	}
	for _, e := range v.Edges {
		if !shown[e.From] || !shown[e.To] {
			t.Errorf("edge %s leaves the view", e.ID)
		}
	}
	// An edge between two shown components is included even when neither is
	// the focus: the imported API depends on the payments service.
	if !slices.ContainsFunc(v.Edges, func(e Edge) bool { return e.From == "HTTP_API:payments-api" && e.To == "SERVICE:payments-api" }) {
		t.Error("the edge between two shown neighbours is missing")
	}
	if !slices.IsSortedFunc(v.Edges, func(a, b Edge) int { return cmpString(a.ID, b.ID) }) {
		t.Error("edges are not in a stable order")
	}
}

func cmpString(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

func TestNeighborhoodIsBoundedAndSaysSo(t *testing.T) {
	m := demoGraph()
	v := mustView(t, m, []string{"TOOL:refund_payment"}, 4, 5, nil)
	if len(v.Nodes) != 5 || !v.Truncated {
		t.Fatalf("limit 5: %d nodes, truncated %v", len(v.Nodes), v.Truncated)
	}
	// Closest first: every node of the first level precedes the next, and
	// within a level kinds come in display order, then keys.
	full := mustView(t, m, []string{"TOOL:refund_payment"}, 1, 0, nil)
	if !slices.Equal(keys(v.Nodes), keys(full.Nodes)[:5]) {
		t.Errorf("truncation keeps %v, want the first 5 of %v", keys(v.Nodes), keys(full.Nodes))
	}
	if v := mustView(t, m, []string{"TOOL:refund_payment"}, 99, 9999, nil); v.Depth != MaxViewDepth {
		t.Errorf("depth capped at %d, got %d", MaxViewDepth, v.Depth)
	}
	if v := mustView(t, m, []string{"TOOL:refund_payment"}, 0, 0, nil); v.Depth != DefaultViewDepth {
		t.Errorf("default depth %d, got %d", DefaultViewDepth, v.Depth)
	}
}

func TestNeighborhoodKindsFilterWhatIsShownAndWalked(t *testing.T) {
	m := demoGraph()
	v := mustView(t, m, []string{"TOOL:refund_payment"}, 3, 0, []Kind{KindService, KindDatabase, KindHTTPAPI})
	for _, n := range v.Nodes[1:] {
		if !slices.Contains([]Kind{KindService, KindDatabase, KindHTTPAPI}, n.Kind) {
			t.Errorf("kind filter shows %s", n.Ref())
		}
	}
	// Not walked through: orders-api is reached only through the agent
	// version and lookup_order, both filtered out.
	if slices.Contains(keys(v.Nodes), "SERVICE:orders-api") {
		t.Error("walked through a filtered kind")
	}
	if !slices.Contains(keys(v.Nodes), "SERVICE:payments-api") {
		t.Error("the tool's own service is missing")
	}
}

func TestNeighborhoodOfNothingIsEmpty(t *testing.T) {
	v, err := Neighborhood(context.Background(), demoGraph(), nil, 0, 0, nil)
	if err != nil || v.Focus == nil || len(v.Nodes) != 0 || len(v.Edges) != 0 {
		t.Fatalf("empty focus: %+v %v", v, err)
	}
	b, _ := json.Marshal(v)
	if string(b) != `{"focus":[],"nodes":[],"edges":[],"depth":2,"truncated":false}` {
		t.Errorf("JSON of an empty view: %s", b)
	}
}

func TestNeighborhoodIsDeterministic(t *testing.T) {
	base := mustView(t, demoGraph(), []string{"AGENT_VERSION:support-refund-agent@1.3.0"}, 2, 12, nil)
	for seed := range uint64(100) {
		m := demoGraph()
		m.shuffle = rand.New(rand.NewPCG(seed, seed))
		v := mustView(t, m, []string{"AGENT_VERSION:support-refund-agent@1.3.0"}, 2, 12, nil)
		if !slices.Equal(keys(v.Nodes), keys(base.Nodes)) || len(v.Edges) != len(base.Edges) {
			t.Fatalf("seed %d: %v, want %v", seed, keys(v.Nodes), keys(base.Nodes))
		}
	}
}

func TestSeedStepHasNoEdge(t *testing.T) {
	m := demoGraph()
	res, err := BlastRadius(context.Background(), m, []Change{{Ref: Ref{KindTool, "refund_payment"}, Change: "modified"}}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	var seed, hop Affected
	for _, a := range res.Affected {
		switch {
		case a.Seed:
			seed = a
		case a.Ref == (Ref{KindService, "payments-api"}):
			hop = a
		}
	}
	b, _ := json.Marshal(seed.Path)
	if string(b) != `[{"component":{"kind":"TOOL","key":"refund_payment"},"label":"refund_payment","edge":null,"direction":null,"confidence":null,"sources":[]}]` {
		t.Errorf("seed path JSON: %s", b)
	}
	b, _ = json.Marshal(hop.Path[1])
	if string(b) != `{"component":{"kind":"SERVICE","key":"payments-api"},"label":"payments-api","edge":"CALLS","direction":"down","confidence":1,"sources":["MANIFEST"]}` {
		t.Errorf("hop JSON: %s", b)
	}
}
