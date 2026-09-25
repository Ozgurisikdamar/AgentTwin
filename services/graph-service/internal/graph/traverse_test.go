package graph

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
)

var ctx = context.Background()

var candidate = &Scope{Agent: "support-refund-agent", Version: "1.3.0"}

func find(t *testing.T, res Result, kind Kind, key string) Affected {
	t.Helper()
	for _, a := range res.Affected {
		if a.Ref.Kind == kind && a.Ref.Key == key {
			return a
		}
	}
	t.Fatalf("%s:%s is not affected; affected: %s", kind, key, refsOf(res.Affected))
	return Affected{}
}

func has(res Result, kind Kind, key string) bool {
	return slices.ContainsFunc(res.Affected, func(a Affected) bool { return a.Ref.Kind == kind && a.Ref.Key == key })
}

func refsOf(as []Affected) string {
	var out []string
	for _, a := range as {
		out = append(out, a.Ref.String())
	}
	return strings.Join(out, ", ")
}

func pathString(steps []Step) string {
	var out []string
	for i, s := range steps {
		if i == 0 {
			out = append(out, s.Label)
			continue
		}
		out = append(out, fmt.Sprintf("-%s(%s)-> %s", s.Edge, s.Direction, s.Label))
	}
	return strings.Join(out, " ")
}

func linked(res Result, key string) *Linked {
	for i := range res.Scenarios {
		if res.Scenarios[i].Ref.Key == key {
			return &res.Scenarios[i]
		}
	}
	return nil
}

var promptChange = Change{Ref: Ref{Kind: KindPrompt, Key: "sha256:bbbb"}, Change: "modified", Summary: "instructions changed",
	Mentions: []string{"refund_payment", "get_refund_policy", "lookup_order"}}

// The golden path (spec §137 step 5): prompt → refund agent → refund_payment
// → payments service, and the refund scenarios with why.
func TestPromptChangeReachesThePaymentsServiceThroughTheTheToolItMentions(t *testing.T) {
	res, err := BlastRadius(ctx, demoGraph(), []Change{promptChange}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	agent := find(t, res, KindAgentVersion, "support-refund-agent@1.3.0")
	if agent.Depth != 1 || agent.Path[1].Direction != "up" || agent.Path[1].Edge != EdgeUses {
		t.Fatalf("the agent is reached going up the USES edge: %+v", agent.Path)
	}
	refund := find(t, res, KindTool, "refund_payment")
	if !refund.Direct || refund.Depth != 2 || refund.Severity != "critical" {
		t.Fatalf("refund_payment: direct %v depth %d severity %s", refund.Direct, refund.Depth, refund.Severity)
	}
	svc := find(t, res, KindService, "payments-api")
	if got, want := pathString(svc.Path), "sha256:bbbb -USES(up)-> support-refund-agent@1.3.0 -USES(down)-> refund_payment -CALLS(down)-> payments-api"; got != want {
		t.Fatalf("path\n got %s\nwant %s", got, want)
	}
	if !svc.Certain || !svc.Direct {
		t.Fatalf("declared path should be certain and direct: %+v", svc)
	}
	find(t, res, KindDatabase, "payments-db")

	// A tool the changed lines do not mention is reached, as indirect and weaker.
	customer := find(t, res, KindTool, "lookup_customer")
	order := find(t, res, KindTool, "lookup_order")
	if customer.Direct || !order.Direct || customer.Score >= order.Score {
		t.Fatalf("lookup_customer (unmentioned) %v %.3f vs lookup_order (mentioned) %v %.3f", customer.Direct, customer.Score, order.Direct, order.Score)
	}
	if !slices.Contains(customer.Factors, "indirect: the agent uses it but the change does not mention it") {
		t.Fatalf("factors: %v", customer.Factors)
	}
	// Going down, USES reaches only the tools the agent acts through: its
	// model is not something it acts on.
	if has(res, KindModel, "scripted/scripted-planner-v1") || has(res, KindRetrieval, "support-kb") {
		t.Fatalf("the model and the knowledge base are inputs, not effects: %s", refsOf(res.Affected))
	}
	// Reading a critical database weighs less than writing one at the same distance.
	if r, w := find(t, res, KindDatabase, "orders-db"), find(t, res, KindDatabase, "payments-db"); r.Depth != w.Depth || r.Score >= w.Score {
		t.Fatalf("orders-db (read) %d %.3f vs payments-db (written) %d %.3f", r.Depth, r.Score, w.Depth, w.Score)
	}
	// A direct link is listed first even when a test of the whole agent
	// scores higher; the weight is the strongest reason.
	pl := linked(res, "refund-policy-lookup")
	if pl == nil || pl.Reasons[0].Via.Key != "get_refund_policy" || !pl.Reasons[0].Direct {
		t.Fatalf("refund-policy-lookup reasons: %+v", pl)
	}
	var strongest float64
	for _, r := range pl.Reasons {
		strongest = max(strongest, r.Score)
	}
	if strongest <= pl.Reasons[0].Score || pl.Weight != round3(strongest*scenarioWeight["medium"]) {
		t.Fatalf("weight %.3f from reasons %+v", pl.Weight, pl.Reasons)
	}
	// The baseline version uses the old prompt: it is not affected.
	if has(res, KindAgentVersion, "support-refund-agent@1.2.4") {
		t.Fatal("the baseline version is not affected by the candidate's prompt")
	}
	// Irreversible actions within reach.
	var irr []string
	for _, a := range res.IrreversibleActions {
		irr = append(irr, a.Ref.Key)
	}
	slices.Sort(irr)
	if !slices.Equal(irr, []string{"export_customer_data", "refund_payment"}) {
		t.Fatalf("irreversible actions: %v", irr)
	}

	// Scenarios: every refund scenario, through refund_payment, directly.
	for _, name := range []string{"refund-timeout-after-mutation", "refund-tool-success-lie", "refund-rate-limited", "refund-happy-path"} {
		l := linked(res, name)
		if l == nil {
			t.Fatalf("%s not selected", name)
		}
		if !l.Reasons[0].Direct || l.Reasons[0].Via.Key == "support-refund-agent" {
			t.Fatalf("%s: first reason should be a directly linked tool, got %+v", name, l.Reasons[0])
		}
	}
	timeout := linked(res, "refund-timeout-after-mutation")
	if got := pathString(timeout.Reasons[0].Path); !strings.HasSuffix(got, "-> refund_payment -TESTED_BY(down)-> refund-timeout-after-mutation") {
		t.Fatalf("scenario path: %s", got)
	}
	// Critical before high; the admin-tool test only through indirect links.
	if res.Scenarios[0].Severity != "critical" || linked(res, "refund-happy-path").Weight >= timeout.Weight {
		t.Fatalf("ranking: %+v", res.Scenarios)
	}
	admin := linked(res, "unauthorized-admin-tool")
	if admin == nil || slices.ContainsFunc(admin.Reasons, func(r Reason) bool { return r.Direct }) {
		t.Fatalf("unauthorized-admin-tool should be linked indirectly only: %+v", admin)
	}
	if admin.Weight >= timeout.Weight {
		t.Fatal("an indirect link weighs less than a direct one")
	}
	// Agent-level tests are reached through the agent.
	mal := linked(res, "malicious-retrieved-content")
	if mal == nil || mal.Reasons[0].Via != (Ref{KindAgent, "support-refund-agent"}) {
		t.Fatalf("malicious-retrieved-content: %+v", mal)
	}
	// The policy guarding refund_payment is impacted.
	if len(res.Policies) != 1 || res.Policies[0].Ref.Key != "refund-limit" {
		t.Fatalf("policies: %+v", res.Policies)
	}
}

func TestPromptWithoutMentionsReachesEveryToolAlike(t *testing.T) {
	c := promptChange
	c.Mentions = nil
	res, err := BlastRadius(ctx, demoGraph(), []Change{c}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range res.Affected {
		if a.Ref.Kind == KindTool && !a.Direct {
			t.Fatalf("%s: without a text to read, no tool is less linked than another", a.Ref)
		}
	}
}

func TestToolChangeReachesItsServicesAndItsAgentButNotTheAgentsOtherTools(t *testing.T) {
	res, err := BlastRadius(ctx, demoGraph(), []Change{{Ref: Ref{KindTool, "refund_payment"}, Change: "modified", Summary: "required argument added"}}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	find(t, res, KindAgentVersion, "support-refund-agent@1.3.0")
	find(t, res, KindService, "payments-api")
	api := find(t, res, KindHTTPAPI, "payments-api")
	if !slices.Equal(api.Path[1].Sources, []Source{SourceOpenAPI}) || api.Path[1].Confidence != 0.9 {
		t.Fatalf("imported edge evidence: %+v", api.Path[1])
	}
	if has(res, KindTool, "lookup_customer") || has(res, KindService, "orders-api") {
		t.Fatalf("a tool change does not reach the agent's other tools: %s", refsOf(res.Affected))
	}
	seed := res.Affected[0]
	if !seed.Seed || seed.Ref.Key != "refund_payment" || seed.Factors[0] != "changed: required argument added" {
		t.Fatalf("seed first: %+v", seed)
	}
}

func TestUnscopedTraversalOnlyEntersTheLatestVersionOfEachAgent(t *testing.T) {
	res, err := BlastRadius(ctx, demoGraph(), []Change{{Ref: Ref{KindService, "payments-api"}, Change: "modified"}}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	find(t, res, KindTool, "refund_payment")
	find(t, res, KindAgentVersion, "support-refund-agent@1.3.0")
	if has(res, KindAgentVersion, "support-refund-agent@1.2.4") {
		t.Fatal("an older version is not live")
	}
}

func TestScopeExcludesOtherVersions(t *testing.T) {
	old := Change{Ref: Ref{KindPrompt, "sha256:aaaa"}, Change: "modified"}
	res, err := BlastRadius(ctx, demoGraph(), []Change{old}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Affected) != 1 || !res.Affected[0].Seed || len(res.Scenarios) != 0 {
		t.Fatalf("only the seed: %s", refsOf(res.Affected))
	}
}

func TestDepthIsBounded(t *testing.T) {
	res, err := BlastRadius(ctx, demoGraph(), []Change{promptChange}, Options{Scope: candidate, MaxDepth: 2})
	if err != nil {
		t.Fatal(err)
	}
	find(t, res, KindTool, "refund_payment")
	if has(res, KindService, "payments-api") {
		t.Fatal("depth 3 is beyond max_depth 2")
	}
	if res.MaxDepth != 2 {
		t.Fatal(res.MaxDepth)
	}
	res, _ = BlastRadius(ctx, demoGraph(), []Change{promptChange}, Options{Scope: candidate, MaxDepth: 99})
	if res.MaxDepth != MaxMaxDepth {
		t.Fatalf("max depth is capped: %d", res.MaxDepth)
	}
}

func TestCyclesTerminateAndVisitEachComponentOnce(t *testing.T) {
	m := newMemory()
	a := m.node(KindService, "a", nil)
	b := m.node(KindService, "b", nil)
	c := m.node(KindService, "c", nil)
	m.edge(a, EdgeDependsOn, b)
	m.edge(b, EdgeDependsOn, c)
	m.edge(c, EdgeDependsOn, a)
	m.edge(a, EdgeDependsOn, a)
	res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindService, "a"}, Change: "modified"}}, Options{MaxDepth: 8})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Affected) != 3 {
		t.Fatalf("each component once: %s", refsOf(res.Affected))
	}
	for _, x := range res.Affected {
		if x.Depth > 1 {
			t.Fatalf("%s reached at depth %d; both directions reach it in one hop", x.Ref, x.Depth)
		}
	}
}

func TestUnknownComponentsAreReportedNotGuessed(t *testing.T) {
	res, err := BlastRadius(ctx, demoGraph(), []Change{{Ref: Ref{KindTool, "issue_voucher"}, Change: "added"}}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Unresolved) != 1 || len(res.Seeds) != 0 || len(res.Affected) != 0 {
		t.Fatalf("%+v", res)
	}
	// Every list is a list, empty or not: clients never meet null.
	b, _ := json.Marshal(res)
	for _, field := range []string{"seeds", "unresolved", "affected", "scenarios", "policies", "evaluators", "irreversible_actions"} {
		var out map[string]json.RawMessage
		_ = json.Unmarshal(b, &out)
		if string(out[field]) == "null" {
			t.Errorf("%s is null", field)
		}
	}
}

func TestInvalidChangesAreRefused(t *testing.T) {
	for _, c := range []Change{
		{Ref: Ref{"WIDGET", "x"}, Change: "modified"},
		{Ref: Ref{KindTool, ""}, Change: "modified"},
		{Ref: Ref{KindTool, "x"}, Change: "renamed"},
		{Ref: Ref{KindTool, "a\nb"}, Change: "modified"},
	} {
		if _, err := BlastRadius(ctx, demoGraph(), []Change{c}, Options{}); err == nil {
			t.Fatalf("accepted %+v", c)
		}
	}
}

func TestTheNodeBudgetTruncates(t *testing.T) {
	m := newMemory()
	hub := m.node(KindService, "hub", nil)
	for i := range 50 {
		m.edge(m.node(KindTool, fmt.Sprintf("t%02d", i), nil), EdgeCalls, hub)
	}
	res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindService, "hub"}, Change: "modified"}}, Options{MaxNodes: 10})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Truncated || len(res.Affected) != 10 {
		t.Fatalf("truncated %v with %d", res.Truncated, len(res.Affected))
	}
}

func TestAnInferredEdgeIsNeverShownAsCertain(t *testing.T) {
	m := newMemory()
	tool := m.node(KindTool, "t", map[string]any{"risk": "READ"})
	svc := m.node(KindService, "s", nil)
	m.edge(tool, EdgeCalls, svc, SourceInferred)
	res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindTool, "t"}, Change: "modified"}}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	s := find(t, res, KindService, "s")
	if s.Certain || !slices.Contains(s.Factors, "reached through an inferred relationship") || s.Path[1].Confidence != 0.5 {
		t.Fatalf("%+v", s)
	}
	if !find(t, res, KindTool, "t").Certain {
		t.Fatal("the seed itself is certain")
	}
}

// randomGraph builds a graph of every kind and edge type, cycles included.
func randomGraph(r *rand.Rand) (*memory, []Change) {
	m := newMemory()
	var ids []string
	n := 3 + r.IntN(40)
	agents := 1 + r.IntN(3)
	for i := range n {
		k := Kinds[r.IntN(len(Kinds))]
		attrs := map[string]any{"risk": []string{"READ", "WRITE_IRREVERSIBLE", "ADMIN", ""}[r.IntN(4)],
			"criticality": []string{"CRITICAL", "LOW", ""}[r.IntN(3)], "severity": "high"}
		if k == KindAgentVersion {
			attrs["agent"] = fmt.Sprintf("agent-%d", r.IntN(agents))
			attrs["version"] = fmt.Sprintf("1.%d", i)
			attrs["latest"] = r.IntN(2) == 0
		}
		ids = append(ids, m.node(k, fmt.Sprintf("n%d", i), attrs))
	}
	for range r.IntN(3 * n) {
		srcs := []Source{Sources[r.IntN(len(Sources))]}
		m.edge(ids[r.IntN(n)], EdgeTypes[r.IntN(len(EdgeTypes))], ids[r.IntN(n)], srcs...)
	}
	var changes []Change
	for range 1 + r.IntN(3) {
		nd := m.nodes[ids[r.IntN(n)]]
		c := Change{Ref: nd.Ref(), Change: ChangeKinds[r.IntN(3)]}
		if r.IntN(2) == 0 {
			c.Mentions = []string{fmt.Sprintf("n%d", r.IntN(n))}
		}
		changes = append(changes, c)
	}
	return m, changes
}

func edgeExists(m *memory, from, to Ref, t EdgeType) bool {
	return slices.ContainsFunc(m.edges, func(e Edge) bool {
		return e.Type == t && m.nodes[e.From].Ref() == from && m.nodes[e.To].Ref() == to
	})
}

// Properties over random graphs: bounded, grounded paths, deterministic,
// monotone in depth, scores in range, only admitted agent versions.
func TestBlastRadiusProperties(t *testing.T) {
	for seed := range uint64(400) {
		r := rand.New(rand.NewPCG(seed, 7))
		m, changes := randomGraph(r)
		depth := 1 + r.IntN(5)
		var scope *Scope
		if r.IntN(3) == 0 {
			scope = &Scope{Agent: "agent-0", Version: "1.1"}
		}
		res, err := BlastRadius(ctx, m, changes, Options{MaxDepth: depth, Scope: scope})
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		seeds := map[Ref]bool{}
		for _, c := range res.Seeds {
			seeds[c.Ref] = true
		}
		for _, a := range res.Affected {
			if a.Depth > depth || a.Depth != len(a.Path)-1 {
				t.Fatalf("seed %d: %s depth %d, path %d, max %d", seed, a.Ref, a.Depth, len(a.Path), depth)
			}
			if !seeds[a.Path[0].Node] {
				t.Fatalf("seed %d: %s path does not start at a seed", seed, a.Ref)
			}
			if a.Score < 0 || a.Score > 1 {
				t.Fatalf("seed %d: score %v", seed, a.Score)
			}
			for i := 1; i < len(a.Path); i++ {
				prev, cur := a.Path[i-1].Node, a.Path[i].Node
				ok := (a.Path[i].Direction == "down" && edgeExists(m, prev, cur, a.Path[i].Edge)) ||
					(a.Path[i].Direction == "up" && edgeExists(m, cur, prev, a.Path[i].Edge))
				if !ok {
					t.Fatalf("seed %d: step %d of %s is not an edge: %+v", seed, i, a.Ref, a.Path)
				}
			}
			if a.Ref.Kind == KindAgentVersion && !a.Seed && !admitted(a.Node, scope) {
				t.Fatalf("seed %d: entered %s outside the scope", seed, a.Ref)
			}
		}
		// Deterministic, whatever order the store answers in.
		m.shuffle = rand.New(rand.NewPCG(seed, 99))
		again, err := BlastRadius(ctx, m, changes, Options{MaxDepth: depth, Scope: scope})
		if err != nil {
			t.Fatal(err)
		}
		a, _ := json.Marshal(res)
		b, _ := json.Marshal(again)
		if string(a) != string(b) {
			t.Fatalf("seed %d: not deterministic\n%s\n%s", seed, a, b)
		}
		m.shuffle = nil
		// Monotone: a deeper bound never loses a component.
		deeper, err := BlastRadius(ctx, m, changes, Options{MaxDepth: depth + 1, Scope: scope})
		if err != nil {
			t.Fatal(err)
		}
		for _, x := range res.Affected {
			if !has(deeper, x.Ref.Kind, x.Ref.Key) {
				t.Fatalf("seed %d: %s lost at depth %d", seed, x.Ref, depth+1)
			}
		}
	}
}

// The store is read one level at a time: a traversal costs a bounded number
// of queries, not one per component.
func TestQueriesAreBoundedByDepthNotBySize(t *testing.T) {
	m := demoGraph()
	if _, err := BlastRadius(ctx, m, []Change{promptChange}, Options{Scope: candidate}); err != nil {
		t.Fatal(err)
	}
	if limit := 1 + DefaultMaxDepth*3 + 3; m.calls > limit {
		t.Fatalf("%d store calls, more than %d", m.calls, limit)
	}
}

func TestTheSameComponentNamedTwiceIsOneSeedWithBothMentions(t *testing.T) {
	first := promptChange
	first.Mentions = append(make([]string, 0, 8), promptChange.Mentions...) // room to append into
	again := promptChange
	again.Mentions = []string{"lookup_customer"}
	res, err := BlastRadius(ctx, demoGraph(), []Change{first, again}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Seeds) != 1 || len(res.Seeds[0].Mentions) != 4 {
		t.Fatalf("seeds %+v", res.Seeds)
	}
	if !find(t, res, KindTool, "lookup_customer").Direct {
		t.Fatal("mentioned in the second change")
	}
	if spare := first.Mentions[:cap(first.Mentions)]; spare[3] != "" {
		t.Fatalf("the caller's slice was written: %v", spare)
	}
}

func TestTheStrongerPathWinsAtTheSameDepth(t *testing.T) {
	for _, order := range [][2]string{{"a", "b"}, {"b", "a"}} {
		m := newMemory()
		x := m.node(KindService, "x", nil)
		agent := m.node(KindAgentVersion, "agent@1", map[string]any{"agent": "agent", "version": "1", "latest": true})
		tools := map[string]string{}
		for _, name := range order {
			tools[name] = m.node(KindTool, name, map[string]any{"risk": "READ"})
			m.edge(agent, EdgeUses, tools[name])
		}
		m.edge(tools["a"], EdgeCalls, x, SourceInferred)
		m.edge(tools["b"], EdgeCalls, x, SourceManifest)
		res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindService, "x"}, Change: "modified"}}, Options{})
		if err != nil {
			t.Fatal(err)
		}
		a := find(t, res, KindAgentVersion, "agent@1")
		if a.Path[1].Node.Key != "b" || !a.Certain {
			t.Fatalf("order %v: the agent is reached through the inferred edge: %s", order, pathString(a.Path))
		}
	}
}

func TestOnlyTheRightKindsAreCollected(t *testing.T) {
	m := demoGraph()
	// Malformed links: a scenario as an evaluator, a service as a test, a
	// service claiming to be a version of the agent.
	m.edge("TOOL:refund_payment", EdgeEvaluatedBy, m.node(KindScenario, "not-an-evaluator", map[string]any{"severity": "critical"}))
	m.edge("TOOL:refund_payment", EdgeTestedBy, "SERVICE:orders-api")
	m.edge("SERVICE:payments-api", EdgeVersionOf, "AGENT:support-refund-agent")
	res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindService, "payments-api"}, Change: "modified"}}, Options{MaxDepth: 1})
	if err != nil {
		t.Fatal(err)
	}
	for _, l := range append(res.Scenarios, res.Evaluators...) {
		if l.Ref.Key == "not-an-evaluator" || l.Ref.Key == "orders-api" {
			t.Fatalf("collected %s", l.Ref)
		}
		for _, r := range l.Reasons {
			if r.Via.Kind == KindAgent {
				t.Fatalf("%s linked through a service posing as an agent version", l.Ref)
			}
		}
	}
}

func TestWhoCallsTheAgentIsAffectedAndPoliciesOfAnyAffectedComponent(t *testing.T) {
	m := demoGraph()
	web := m.node(KindService, "support-web", nil)
	m.edge(web, EdgeCalls, "AGENT_VERSION:support-refund-agent@1.3.0")
	freeze := m.node(KindPolicy, "payments-change-freeze", nil)
	m.edge("SERVICE:payments-api", EdgeGuardedBy, freeze)
	res, err := BlastRadius(ctx, m, []Change{{Ref: Ref{KindTool, "refund_payment"}, Change: "modified"}}, Options{Scope: candidate})
	if err != nil {
		t.Fatal(err)
	}
	w := find(t, res, KindService, "support-web")
	if w.Depth != 2 || w.Path[2].Direction != "up" {
		t.Fatalf("support-web: %s", pathString(w.Path))
	}
	if !slices.ContainsFunc(res.Policies, func(l Linked) bool { return l.Ref.Key == "payments-change-freeze" }) {
		t.Fatalf("policies: %+v", res.Policies)
	}
}
