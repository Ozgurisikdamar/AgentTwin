package impact

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

var (
	base = Version{Agent: "support-refund-agent", Version: "1.2.4", Tools: []domain.Tool{
		{Name: "refund_payment", Description: "Issue a refund for an order payment.", Risk: domain.RiskWriteIrreversible},
		{Name: "legacy_lookup", Description: "Look up an order in the old system.", Risk: domain.RiskRead},
	}}
	candidate = Version{Agent: "support-refund-agent", Version: "1.3.0", Tools: []domain.Tool{
		{Name: "refund_payment", Description: "Refund part or all of an order's payment, once.", Risk: domain.RiskWriteIrreversible},
		{Name: "export_customer_data", Description: "Export all personal data of a customer.", Risk: domain.RiskAdmin},
	}}
)

// stored reads items back as the control plane stores them (JSON).
func stored(t *testing.T, items []changes.Item) []changes.Item {
	t.Helper()
	raw, err := json.Marshal(items)
	if err != nil {
		t.Fatal(err)
	}
	var out []changes.Item
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func promptItem(diff []changes.DiffLine, mentions []string) changes.Item {
	d := map[string]any{"base_sha256": "a", "candidate_sha256": "b"}
	if diff != nil {
		d["diff"] = diff
		d["mentions"] = mentions
	}
	return changes.Item{Kind: "prompt", Subject: "b", Change: "modified", Summary: "prompt modified", Detail: d}
}

func TestSeedsCollapseBeyondTheBound(t *testing.T) {
	var seeds []changes.Seed
	for i := range MaxSeeds {
		seeds = append(seeds, changes.Seed{Component: changes.Ref{Kind: "TOOL", Key: fmt.Sprintf("t%d", i)}, Change: "added"})
	}
	got, note := Seeds(seeds, candidate)
	if !reflect.DeepEqual(got, seeds) || note != "" {
		t.Fatalf("within the bound the seeds are kept: %d %q", len(got), note)
	}
	seeds = append(seeds, changes.Seed{Component: changes.Ref{Kind: "TOOL", Key: "one-more"}, Change: "added"})
	got, note = Seeds(seeds, candidate)
	want := []changes.Seed{{Component: changes.Ref{Kind: "AGENT_VERSION", Key: "support-refund-agent@1.3.0"},
		Change: "modified", Summary: "101 changed components"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("seeds = %+v", got)
	}
	if !strings.Contains(note, "101 changed components") || !strings.Contains(note, "candidate version") {
		t.Fatalf("note = %q", note)
	}
}

func TestQueriesDescribeWhatChanged(t *testing.T) {
	items := []changes.Item{
		promptItem([]changes.DiffLine{
			{Op: " ", Text: "You are a support agent."},
			{Op: "+", Text: "Refunds above 100 USD go to a human specialist."},
			{Op: "-", Text: "Refund up to 500 USD on your own."},
			{Op: "+", Text: "   "},
		}, []string{"refund_payment"}),
		{Kind: "model", Subject: "anthropic/x", Change: "modified", Summary: "model changed", Detail: map[string]any{}},
		{Kind: "tool", Subject: "refund_payment", Change: "modified", Summary: "description changed", Detail: map[string]any{}},
		{Kind: "tool", Subject: "legacy_lookup", Change: "removed", Summary: "tool removed", Detail: map[string]any{}},
		{Kind: "tool", Subject: "export_customer_data", Change: "added", Summary: "tool added (ADMIN)", Detail: map[string]any{}},
		{Kind: "retrieval_source", Subject: "support-kb", Change: "added", Summary: "retrieval source added"},
		{Kind: "policy", Subject: "refund-limit", Change: "modified", Summary: "limit lowered to 100 USD"},
		{Kind: "code", Subject: "abc1234", Change: "modified", Summary: "code changed",
			Detail: map[string]any{"changed_files": []string{"src/refunds/limits.py", "README.md"}}},
		{Kind: "limits", Subject: "support-refund-agent", Change: "modified", Summary: "max steps 8 → 12"},
	}
	want := []Query{
		{ID: "item-1", Text: "Refunds above 100 USD go to a human specialist.\nRefund up to 500 USD on your own.\nrefund_payment"},
		{ID: "item-2", Text: "tool refund_payment modified: description changed\nRefund part or all of an order's payment, once."},
		// A removed tool is described as the base knew it.
		{ID: "item-3", Text: "tool legacy_lookup removed: tool removed\nLook up an order in the old system."},
		{ID: "item-4", Text: "tool export_customer_data added: tool added (ADMIN)\nExport all personal data of a customer."},
		{ID: "item-5", Text: "retrieval source support-kb added"},
		{ID: "item-6", Text: "policy refund-limit modified: limit lowered to 100 USD"},
		{ID: "item-7", Text: "src/refunds/limits.py\nREADME.md"},
	}
	// Items computed here and items read back from storage ask the same.
	for name, in := range map[string][]changes.Item{"computed": items, "stored": stored(t, items)} {
		got, note := Queries(in, base, candidate)
		if note != "" {
			t.Fatalf("%s: note %q", name, note)
		}
		if len(got) != len(want) {
			t.Fatalf("%s: %d queries: %+v", name, len(got), got)
		}
		for i := range want {
			if got[i].ID != want[i].ID || got[i].Text != want[i].Text {
				t.Errorf("%s: query %d = %q %q", name, i, got[i].ID, got[i].Text)
			}
		}
		if *got[1].Item != (ItemRef{Kind: "tool", Subject: "refund_payment", Change: "modified"}) {
			t.Fatalf("%s: item = %+v", name, got[1].Item)
		}
	}
	// Only hashes of the prompt are known: nothing to compare.
	if got, _ := Queries([]changes.Item{promptItem(nil, nil)}, base, candidate); len(got) != 0 {
		t.Fatalf("hash-only prompt: %+v", got)
	}
	// The text sent is not echoed: the JSON of a query carries its id and text only.
	raw, _ := json.Marshal(want[0])
	if string(raw) != `{"id":"item-1","text":"Refunds above 100 USD go to a human specialist.\nRefund up to 500 USD on your own.\nrefund_payment"}` {
		t.Fatalf("query JSON = %s", raw)
	}
}

func TestQueriesAreBounded(t *testing.T) {
	// Two bytes each, after an odd number of bytes: the bound falls inside a character.
	long := "x" + strings.Repeat("é", MaxQueryText)
	items := []changes.Item{{Kind: "policy", Subject: "p", Change: "added", Summary: long}}
	for i := range MaxQueries + 5 {
		items = append(items, changes.Item{Kind: "dependency", Subject: fmt.Sprintf("d%d", i), Change: "added", Summary: "x"})
	}
	got, note := Queries(items, base, candidate)
	if len(got) != MaxQueries || got[MaxQueries-1].ID != fmt.Sprintf("item-%d", MaxQueries) {
		t.Fatalf("%d queries", len(got))
	}
	if n := len(got[0].Text); n > MaxQueryText || !utf8.ValidString(got[0].Text) || n < MaxQueryText-1 {
		t.Fatalf("bounded text: %d bytes, valid %v", n, utf8.ValidString(got[0].Text))
	}
	if note != "6 changed items were not compared with scenario descriptions (at most 50 are)." {
		t.Fatalf("note = %q", note)
	}
}

func graphFixture() *Graph {
	// path is the graph's path: the changed component first, the scenario last.
	path := func(steps ...string) []json.RawMessage {
		var p []json.RawMessage
		for _, st := range steps {
			kind, key, _ := strings.Cut(st, ":")
			p = append(p, json.RawMessage(fmt.Sprintf(`{"component":{"kind":%q,"key":%q},"label":%q}`, kind, key, key)))
		}
		return p
	}
	aff := func(key string, score float64) Affected {
		return Affected{Component: changes.Ref{Kind: "TOOL", Key: key}, Label: key, Score: score, Attributes: map[string]any{}}
	}
	g := &Graph{
		Seeds: []changes.Seed{{Component: changes.Ref{Kind: "PROMPT", Key: "b"}, Change: "modified"}},
		Scenarios: []GraphLinked{
			// A test of the changed tool itself.
			{Component: changes.Ref{Kind: "SCENARIO", Key: "refund-happy-path"}, Weight: 1, Reasons: []GraphReason{
				{Via: changes.Ref{Kind: "TOOL", Key: "refund_payment"}, ViaLabel: "refund_payment", Direct: true, Score: 0.9,
					Path: path("TOOL:refund_payment", "SCENARIO:refund-happy-path")},
			}},
			// A test of a tool the changed prompt reaches through the version.
			{Component: changes.Ref{Kind: "SCENARIO", Key: "refund-over-limit"}, Weight: 0.5, Reasons: []GraphReason{
				{Via: changes.Ref{Kind: "TOOL", Key: "escalate_to_human"}, ViaLabel: "escalate_to_human", Direct: false, Score: 0.4,
					Path: path("PROMPT:b", "AGENT_VERSION:support-refund-agent@1.3.0", "TOOL:escalate_to_human", "SCENARIO:refund-over-limit")},
				{Via: changes.Ref{Kind: "TOOL", Key: "lookup_order"}, ViaLabel: "lookup_order", Direct: true, Score: 0.3,
					Path: path("PROMPT:b", "TOOL:lookup_order", "SCENARIO:refund-over-limit")},
			}},
			{Component: changes.Ref{Kind: "SCENARIO", Key: "retired-scenario"}, Weight: 0.3, Reasons: []GraphReason{
				{Via: changes.Ref{Kind: "TOOL", Key: "refund_payment"}, Direct: true, Score: 0.3,
					Path: path("TOOL:refund_payment", "SCENARIO:retired-scenario")},
			}},
		},
		IrreversibleActions: []Affected{aff("refund_payment", 0.9)},
		MaxDepth:            4,
	}
	for i := range MaxAffectedShown + 10 {
		g.Affected = append(g.Affected, aff(fmt.Sprintf("c%02d", i), float64(i)/100))
	}
	return g
}

func matched(name, severity, source string, tags []string, nameMatch bool, matchedTags []string, sourceMatch bool, sim ...Similarity) MatchedScenario {
	ms := MatchedScenario{ID: "id-" + name, Name: name, Severity: severity, Tags: tags, Source: source, LatestVersion: 1,
		Description: name + " description"}
	ms.Matched.Name, ms.Matched.Tags, ms.Matched.Source, ms.Matched.Similar = nameMatch, matchedTags, sourceMatch, sim
	return ms
}

func TestMergeGivesEveryScenarioEveryReason(t *testing.T) {
	g := graphFixture()
	queries := []Query{
		{ID: "item-1", Item: &ItemRef{Kind: "prompt", Subject: "b", Change: "modified"}},
		{ID: "item-2", Item: &ItemRef{Kind: "tool", Subject: "export_customer_data", Change: "added"}},
	}
	m := &Match{
		EmbeddingModel: "hashing-v1", MinSimilarity: 0.25, UnknownNames: []string{"retired-scenario"},
		Scenarios: []MatchedScenario{
			matched("refund-happy-path", "high", "manual", []string{"refunds"}, true, nil, false),
			matched("refund-over-limit", "critical", "manual", []string{"refunds"}, true, nil, false,
				Similarity{Query: "item-1", Similarity: 0.45}),
			matched("unauthorized-admin-tool", "critical", "manual", []string{"security"}, false, []string{"security"}, false,
				Similarity{Query: "item-2", Similarity: 0.61}),
			matched("refund-charged-twice", "medium", KnownRegressionSource, []string{}, false, nil, true),
			// Matched by source, but not a regression's: nothing this impact asked for.
			matched("imported-case", "low", "imported", nil, false, nil, true),
			// Similar only to a query this impact did not ask: no reason, not selected.
			matched("stray", "low", "manual", nil, false, nil, false, Similarity{Query: "item-9", Similarity: 0.9}),
		},
	}
	items := stored(t, []changes.Item{
		{Kind: "tool", Subject: "export_customer_data", Change: "added", Detail: map[string]any{"risk": domain.RiskAdmin, "new_privilege": true}},
	})
	got := Merge(items, g, m, queries)

	names := []string{}
	for _, s := range got.Scenarios {
		names = append(names, s.Name)
	}
	// Severest first, then by name; the stray scenario is not part of it.
	if want := []string{"refund-over-limit", "unauthorized-admin-tool", "refund-happy-path", "refund-charged-twice"}; !reflect.DeepEqual(names, want) {
		t.Fatalf("scenarios = %v", names)
	}
	by := map[string]Scenario{}
	for _, s := range got.Scenarios {
		by[s.Name] = s
		if !s.InLibrary || s.ID != "id-"+s.Name || s.Description == "" {
			t.Fatalf("%s: library fields %+v", s.Name, s)
		}
	}
	happy := by["refund-happy-path"].Reasons
	tool := changes.Ref{Kind: "TOOL", Key: "refund_payment"}
	if len(happy.Graph) != 1 || !happy.Graph[0].Direct || happy.Graph[0].Hops != 1 || happy.Graph[0].Via != tool ||
		happy.Graph[0].From != tool || len(happy.Graph[0].Path) != 2 {
		t.Fatalf("graph reason = %+v", happy.Graph)
	}
	over := by["refund-over-limit"]
	g0 := over.Reasons.Graph[0]
	if len(over.Reasons.Graph) != 2 || g0.Direct || g0.Hops != 3 || g0.From != (changes.Ref{Kind: "PROMPT", Key: "b"}) ||
		g0.Via.Key != "escalate_to_human" {
		t.Fatalf("reached reasons = %+v", over.Reasons.Graph)
	}
	if want := []ScenarioSimilarity{{Item: ItemRef{Kind: "prompt", Subject: "b", Change: "modified"}, Similarity: 0.45}}; !reflect.DeepEqual(over.Reasons.Similar, want) {
		t.Fatalf("similar = %+v", over.Reasons.Similar)
	}
	// The weaker link is in the reasons, not in the sentences: there is a stronger one.
	wantWhy := []string{
		"tests tool lookup_order, which the change to the prompt reaches directly",
		"its description is close to the change of the prompt (similarity 0.45)",
	}
	if !reflect.DeepEqual(over.Why, wantWhy) {
		t.Fatalf("why = %q", over.Why)
	}
	if w := by["refund-happy-path"].Why; !reflect.DeepEqual(w, []string{"tests tool refund_payment, which changed"}) {
		t.Fatalf("why = %q", w)
	}
	admin := by["unauthorized-admin-tool"]
	if !reflect.DeepEqual(admin.Reasons.AlwaysRunTags, []string{"security"}) || len(admin.Reasons.Graph) != 0 ||
		!reflect.DeepEqual(admin.Why, []string{
			"its description is close to the change of tool export_customer_data (similarity 0.61)",
			"always runs (tagged security)",
		}) {
		t.Fatalf("always-run = %+v", admin)
	}
	reg := by["refund-charged-twice"]
	if !reg.Reasons.KnownRegression || !reflect.DeepEqual(reg.Why, []string{"a known production regression"}) {
		t.Fatalf("regression = %+v", reg)
	}
	if want := (Counts{Scenarios: 4, Graph: 2, Similar: 2, AlwaysRun: 1, KnownRegression: 1}); got.Counts != want {
		t.Fatalf("counts = %+v", got.Counts)
	}
	if !reflect.DeepEqual(got.UnlinkedScenarios, []string{"retired-scenario"}) || got.EmbeddingModel != "hashing-v1" || got.MinSimilarity != 0.25 {
		t.Fatalf("unlinked/model = %v %q %v", got.UnlinkedScenarios, got.EmbeddingModel, got.MinSimilarity)
	}
	// The graph summary: the most affected components first, bounded.
	if got.Graph == nil || got.Graph.AffectedCount != MaxAffectedShown+10 || len(got.Graph.Affected) != MaxAffectedShown ||
		got.Graph.Affected[0].Component.Key != fmt.Sprintf("c%02d", MaxAffectedShown+9) || got.Graph.MaxDepth != 4 {
		t.Fatalf("graph summary = %+v", got.Graph)
	}
	if len(got.IrreversibleActions) != 1 || got.IrreversibleActions[0].Component.Key != "refund_payment" {
		t.Fatalf("irreversible = %+v", got.IrreversibleActions)
	}
	if want := []Privilege{{Tool: "export_customer_data", Change: "added", Risk: domain.RiskAdmin}}; !reflect.DeepEqual(got.NewPrivileges, want) {
		t.Fatalf("privileges = %+v", got.NewPrivileges)
	}
}

func TestWeakerLinksAreSaidWhenThereIsNoStrongerOne(t *testing.T) {
	weak := ScenarioGraphReason{From: changes.Ref{Kind: "PROMPT", Key: "b"}, FromLabel: "b",
		Via: changes.Ref{Kind: "AGENT", Key: "support-refund-agent"}, ViaLabel: "support-refund-agent", Hops: 3}
	got := why(Reasons{Graph: []ScenarioGraphReason{weak}})
	want := []string{"tests agent support-refund-agent, which the change to the prompt reaches in 2 steps " +
		"(a weaker link: through the agent, or a tool the change does not name)"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("why = %q", got)
	}
}

func TestMergeWithoutTheLibraryOrTheGraph(t *testing.T) {
	g := graphFixture()
	// The simulation service did not answer: the graph's links are all there is.
	got := Merge(nil, g, nil, nil)
	if len(got.Scenarios) != 3 || got.EmbeddingModel != "" {
		t.Fatalf("graph only: %+v", got.Scenarios)
	}
	for _, s := range got.Scenarios {
		if s.InLibrary || s.ID != "" || len(s.Reasons.Graph) == 0 || len(s.Why) == 0 {
			t.Fatalf("graph-only scenario %+v", s)
		}
	}
	if got.Scenarios[0].Severity != "medium" {
		t.Fatalf("a scenario without a known severity is medium: %+v", got.Scenarios[0])
	}
	// The graph did not answer: no graph reasons, no graph summary.
	m := &Match{Scenarios: []MatchedScenario{
		matched("a", "low", "manual", []string{"security"}, false, []string{"security"}, false),
		matched("b", "low", "manual", nil, true, nil, false), // a name without a link: nothing to say
	}}
	got = Merge(nil, nil, m, nil)
	if got.Graph != nil || len(got.Scenarios) != 1 || got.Scenarios[0].Name != "a" {
		t.Fatalf("library only: %+v %+v", got.Graph, got.Scenarios)
	}
	// Nothing at all: empty lists, not nulls.
	raw, _ := json.Marshal(Merge(nil, nil, nil, nil))
	for _, field := range []string{`"scenarios":[]`, `"unlinked_scenarios":[]`, `"irreversible_actions":[]`, `"new_privileges":[]`, `"notes":[]`, `"graph":null`} {
		if !strings.Contains(string(raw), field) {
			t.Fatalf("%s missing in %s", field, raw)
		}
	}
}

func TestNewMatchRequestAsksForWhatThePolicySays(t *testing.T) {
	g := graphFixture()
	pol := Policy{AlwaysRunTags: []string{"critical", "security"}, IncludeKnownRegressions: true, MaxDepth: 4}
	q := []Query{{ID: "item-1", Text: "x"}}
	req, note := NewMatchRequest("p1", "support-refund-agent", g, pol, q)
	want := MatchRequest{ProjectID: "p1", Agent: "support-refund-agent",
		Names: []string{"refund-happy-path", "refund-over-limit", "retired-scenario"},
		Tags:  []string{"critical", "security"}, Sources: []string{KnownRegressionSource}, Queries: q}
	if !reflect.DeepEqual(req, want) || note != "" {
		t.Fatalf("request = %+v %q", req, note)
	}
	req, _ = NewMatchRequest("p1", "a", nil, Policy{}, nil)
	raw, _ := json.Marshal(req)
	if string(raw) != `{"project_id":"p1","agent":"a","names":[],"tags":[],"sources":[],"queries":[]}` {
		t.Fatalf("empty request = %s", raw)
	}
	big := &Graph{}
	for i := range MaxNamed + 3 {
		big.Scenarios = append(big.Scenarios, GraphLinked{Component: changes.Ref{Kind: "SCENARIO", Key: fmt.Sprintf("s%d", i)}})
	}
	req, note = NewMatchRequest("p1", "a", big, Policy{}, nil)
	if len(req.Names) != MaxNamed || req.Names[0] != "s0" || !strings.Contains(note, "503 scenarios") {
		t.Fatalf("bounded names: %d %q", len(req.Names), note)
	}
}

func TestNewPrivileges(t *testing.T) {
	items := []changes.Item{
		{Kind: "tool", Subject: "lookup", Change: "added", Detail: map[string]any{"risk": domain.RiskRead, "new_privilege": false}},
		{Kind: "tool", Subject: "refund", Change: "added", Detail: map[string]any{"risk": domain.RiskWriteIrreversible, "new_privilege": true}},
		{Kind: "tool", Subject: "notes", Change: "modified", Detail: map[string]any{
			"risk": map[string]any{"from": domain.RiskRead, "to": domain.RiskWriteReversible, "escalated": true}}},
		{Kind: "tool", Subject: "calmer", Change: "modified", Detail: map[string]any{
			"risk": map[string]any{"from": domain.RiskAdmin, "to": domain.RiskRead, "escalated": false}}},
		{Kind: "tool", Subject: "gone", Change: "removed", Detail: map[string]any{"risk": domain.RiskAdmin}},
		{Kind: "policy", Subject: "x", Change: "added", Detail: map[string]any{"new_privilege": true}},
	}
	want := []Privilege{
		{Tool: "refund", Change: "added", Risk: domain.RiskWriteIrreversible},
		{Tool: "notes", Change: "escalated", Risk: domain.RiskWriteReversible, From: domain.RiskRead},
	}
	for name, in := range map[string][]changes.Item{"computed": items, "stored": stored(t, items)} {
		if got := NewPrivileges(in); !reflect.DeepEqual(got, want) {
			t.Fatalf("%s: %+v", name, got)
		}
	}
}
