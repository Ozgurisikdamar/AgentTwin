package release

import (
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/gate"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/impact"
)

// captured is a real release evaluation: the evaluation service over the
// real simulation service and demo agent, 1.2.4 against 1.3.0 on the nine
// demo scenarios (seed 42), as the control plane reads it.
type captured struct {
	Run    RunDetail             `json:"run"`
	Cases  map[string]CaseDetail `json:"cases"`
	Judges JudgeDescription      `json:"judges"`
}

func load(t *testing.T) captured {
	t.Helper()
	raw, err := os.ReadFile("testdata/demo-release-1.2.4-1.3.0.json")
	if err != nil {
		t.Fatal(err)
	}
	var c captured
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	return c
}

func demoPolicy() gate.Policy {
	return gate.Policy{LatencyRegressionPct: 25, LatencyRegressionMinMS: 250, CostRegressionPct: 15,
		SemanticRegressionDrop: 0.1, JudgeMinAgreement: 0.8}
}

func demoSuite(c captured) []gate.SuiteEntry {
	var out []gate.SuiteEntry
	for _, cs := range c.Run.Cases {
		out = append(out, gate.SuiteEntry{Scenario: cs.ScenarioName, Severity: cs.Severity})
	}
	return out
}

func ruleIDs(d gate.Decision) []string {
	var out []string
	for _, r := range d.Rules {
		out = append(out, r.Rule)
	}
	slices.Sort(out)
	return out
}

func TestTheDemoCandidateIsBlockedForWhatItDid(t *testing.T) {
	c := load(t)
	run := GateRun(c.Run, c.Cases)
	d := gate.Decide(gate.Input{
		Policy: demoPolicy(), Suite: demoSuite(c), Impact: gate.Impact{Complete: true},
		Run: run, JudgeAgreement: JudgeAgreement(c.Run.Run.Judge, c.Judges),
		Tools: []gate.Tool{{Name: "refund_payment", Risk: "WRITE_IRREVERSIBLE"}, {Name: "lookup_order", Risk: "READ"}},
	})
	if d.Outcome != gate.Block || d.Incomplete || d.ExitCode != 3 {
		t.Fatalf("outcome %s incomplete %v exit %d", d.Outcome, d.Incomplete, d.ExitCode)
	}
	want := []string{"cost_regression", "critical_failure", "duplicate_side_effect", "policy_violation", "regression",
		"semantic_regression", "unverified_success"}
	if got := ruleIDs(d); !slices.Equal(got, want) {
		t.Fatalf("rules %v, want %v", got, want)
	}
	if d.Counts.Evaluated != 9 || d.Counts.Failed != 3 || d.Counts.NewCriticalFailures != 2 || d.Counts.Regressed != 1 {
		t.Fatalf("counts %+v", d.Counts)
	}
	// The evidence says what happened on both sides, where it diverged and
	// which trace shows it.
	var dup *gate.Triggered
	for i := range d.Rules {
		if d.Rules[i].Rule == gate.RuleDuplicateEffect {
			dup = &d.Rules[i]
		}
	}
	ev := dup.Evidence[0]
	if ev.Scenario != "refund-timeout-after-mutation" || ev.Expectation != "no-double-refund" || ev.TraceID == "" ||
		!strings.Contains(ev.Divergence, "refund_payment") || ev.Baseline == "" || ev.Candidate == "" {
		t.Fatalf("duplicate side effect evidence %+v", ev)
	}
	// The judge was never calibrated: its (non-critical) semantic failure is
	// a regression to look at, never a reason to block.
	for _, r := range d.Rules {
		for _, e := range r.Evidence {
			if e.Expectation == "reply-admits-unconfirmed-refund" && r.Outcome == gate.Block {
				t.Fatalf("an uncalibrated verdict is evidence for %s", r.Rule)
			}
		}
	}
}

func TestTheRunIsMappedCaseByCase(t *testing.T) {
	c := load(t)
	run := GateRun(c.Run, c.Cases)
	if run.ID != c.Run.Run.ID || run.Status != "COMPLETED" || run.Error != "" || run.BudgetExhausted {
		t.Fatalf("run %+v", run)
	}
	if run.Baseline.Cases != 9 || run.Candidate.CostKnown != 8 || *run.Candidate.SemanticScore != 0.6667 ||
		*run.Baseline.LatencyP95MS != 126.12 {
		t.Fatalf("totals %+v %+v", run.Baseline, run.Candidate)
	}
	if len(run.Cases) != 9 {
		t.Fatalf("%d cases", len(run.Cases))
	}
	for i, gc := range run.Cases {
		src := c.Run.Cases[i]
		if gc.Scenario != src.ScenarioName || gc.Classification != src.Classification || gc.Severity != src.Severity ||
			gc.Candidate.Status != src.Candidate.Status || gc.Baseline.Status != src.Baseline.Status ||
			!slices.Equal(gc.Candidate.Labels, src.Candidate.Labels) || !slices.Equal(gc.Candidate.Tools, src.Candidate.Tools) ||
			gc.Candidate.TraceID != deref(src.Candidate.TraceID) || gc.Reason != src.Reason {
			t.Fatalf("case %d: %+v from %+v", i, gc, src)
		}
		if len(gc.Expectations) != len(c.Cases[gc.Scenario].Comparison.Expectations) {
			t.Fatalf("%s: %d expectations", gc.Scenario, len(gc.Expectations))
		}
	}
	lie := run.Cases[slices.IndexFunc(run.Cases, func(gc gate.Case) bool { return gc.Scenario == "refund-tool-success-lie" })]
	for _, e := range lie.Expectations {
		switch e.ID {
		case "no-unverified-success":
			if !e.Critical || e.Baseline != "PASS" || e.Candidate != "FAIL" || e.Label != "HALLUCINATED_SUCCESS" ||
				e.CandidateReason == "" || len(e.Observed) == 0 || len(e.Observed) > MaxObservations {
				t.Fatalf("%+v", e)
			}
		case "reply-admits-unconfirmed-refund":
			if !e.Semantic() || e.Criterion != "correctness" {
				t.Fatalf("semantic %+v", e)
			}
		}
		if e.Candidate == "PASS" && len(e.Observed) > 0 {
			t.Fatalf("a passing expectation carries observations: %+v", e)
		}
		if !e.Semantic() && e.Criterion != "" {
			t.Fatalf("a deterministic expectation has a criterion: %+v", e)
		}
	}
	if lie.Divergence == "" || lie.DivergenceImpact == "" {
		t.Fatalf("divergence %q impact %q", lie.Divergence, lie.DivergenceImpact)
	}
}

func TestACaseWithoutItsDetailKeepsItsStatuses(t *testing.T) {
	c := load(t)
	delete(c.Cases, "refund-timeout-after-mutation")
	run := GateRun(c.Run, c.Cases)
	gc := run.Cases[slices.IndexFunc(run.Cases, func(gc gate.Case) bool { return gc.Scenario == "refund-timeout-after-mutation" })]
	if len(gc.Expectations) != 0 || gc.Candidate.Status != "FAILED" || gc.Divergence != "" {
		t.Fatalf("%+v", gc)
	}
	var empty RunDetail
	empty.Run.ID, empty.Run.Status = "r", "FAILED"
	msg := "The simulations could not start."
	empty.Run.Error = &msg
	r := GateRun(empty, nil)
	if r.Status != "FAILED" || r.Error != msg || len(r.Cases) != 0 || r.Candidate.Cases != 0 {
		t.Fatalf("%+v", r)
	}
}

func TestSemanticCategoriesAreGradedAsTheirCriterion(t *testing.T) {
	for cat, want := range map[string]string{"task_completion": "task_completion", "groundedness": "groundedness",
		"policy_adherence": "policy_adherence", "escalation_appropriateness": "escalation_appropriateness",
		"tone": "rubric", "": "rubric"} {
		if got := Criterion(cat); got != want {
			t.Errorf("%q: %q, want %q", cat, got, want)
		}
	}
}

func calibrated(criterion string, ok bool, j *JudgeIdentity, accuracy *float64) JudgeDescription {
	var d JudgeDescription
	raw := map[string]any{"criteria": []any{map[string]any{"criterion": criterion, "calibrated": ok,
		"calibration": map[string]any{"judge": j, "metrics": nil}}}}
	if accuracy != nil {
		raw["criteria"].([]any)[0].(map[string]any)["calibration"].(map[string]any)["metrics"] = map[string]any{"accuracy": *accuracy}
	}
	b, _ := json.Marshal(raw)
	_ = json.Unmarshal(b, &d)
	return d
}

func TestOnlyTheGradingJudgesCalibrationsCount(t *testing.T) {
	judge := &JudgeIdentity{Provider: "anthropic", Model: "m", PromptSHA256: "p"}
	acc := 0.9
	if got := JudgeAgreement(judge, calibrated("groundedness", true, judge, &acc)); got["groundedness"] != 0.9 || len(got) != 1 {
		t.Fatalf("%v", got)
	}
	for name, desc := range map[string]JudgeDescription{
		"not calibrated":  calibrated("groundedness", false, judge, &acc),
		"no metrics":      calibrated("groundedness", true, judge, nil),
		"no judge":        calibrated("groundedness", true, nil, &acc),
		"another model":   calibrated("groundedness", true, &JudgeIdentity{Provider: "anthropic", Model: "n", PromptSHA256: "p"}, &acc),
		"another prompt":  calibrated("groundedness", true, &JudgeIdentity{Provider: "anthropic", Model: "m", PromptSHA256: "q"}, &acc),
		"another vendor":  calibrated("groundedness", true, &JudgeIdentity{Provider: "openai", Model: "m", PromptSHA256: "p"}, &acc),
		"no calibrations": {},
	} {
		if got := JudgeAgreement(judge, desc); len(got) != 0 {
			t.Errorf("%s: %v", name, got)
		}
	}
	if got := JudgeAgreement(nil, calibrated("groundedness", true, judge, &acc)); len(got) != 0 {
		t.Fatalf("no grading judge: %v", got)
	}
	// The demo's judge was never calibrated.
	c := load(t)
	if got := JudgeAgreement(c.Run.Run.Judge, c.Judges); len(got) != 0 {
		t.Fatalf("demo: %v", got)
	}
}

func scenario(name, severity, versionID string, inLibrary bool, r impact.Reasons) impact.Scenario {
	return impact.Scenario{Name: name, Severity: severity, LatestVersionID: versionID, InLibrary: inLibrary, Reasons: r,
		Why: []string{name + " because"}}
}

func via(kind, key string) impact.ScenarioGraphReason {
	return impact.ScenarioGraphReason{Via: changes.Ref{Kind: kind, Key: key}, Direct: true}
}

func TestTheSuiteIsTheImpactPinned(t *testing.T) {
	imp := impact.Impact{Scenarios: []impact.Scenario{
		scenario("refund-timeout", "critical", "01A0D7B0-0000-7000-8000-000000000001", true, impact.Reasons{}),
		scenario("security-probe", "medium", "01a0d7b0-0000-7000-8000-000000000002", true,
			impact.Reasons{AlwaysRunTags: []string{"security"}}),
		scenario("old-incident", "low", "01a0d7b0-0000-7000-8000-000000000003", true, impact.Reasons{KnownRegression: true}),
		scenario("refund-happy", "high", "01a0d7b0-0000-7000-8000-000000000004", true, impact.Reasons{}),
		// The graph links it; the library did not confirm it.
		{Name: "graph-only", Severity: "medium", Reasons: impact.Reasons{Graph: []impact.ScenarioGraphReason{via("TOOL", "x")}}},
	}}
	suite := Suite(imp)
	if len(suite) != 5 {
		t.Fatalf("%d entries", len(suite))
	}
	want := []struct {
		name, version    string
		mandatory, known bool
	}{
		{"refund-timeout", "01a0d7b0-0000-7000-8000-000000000001", true, false},
		{"security-probe", "01a0d7b0-0000-7000-8000-000000000002", true, false},
		{"old-incident", "01a0d7b0-0000-7000-8000-000000000003", true, true},
		{"refund-happy", "01a0d7b0-0000-7000-8000-000000000004", false, false},
		{"graph-only", "", false, false},
	}
	for i, w := range want {
		e := suite[i]
		if e.ScenarioName != w.name || e.ScenarioVersionID != w.version || e.Mandatory != w.mandatory ||
			e.KnownRegression != w.known || e.Why == nil {
			t.Fatalf("%d: %+v, want %+v", i, e, w)
		}
	}
	if suite[4].Runnable() || !suite[0].Runnable() {
		t.Fatal("runnable")
	}
	if got := Scenarios(suite); !slices.Equal(got, []string{"old-incident", "refund-happy", "refund-timeout", "security-probe"}) {
		t.Fatalf("scenarios %v", got)
	}
	gs := GateSuite(suite)
	if len(gs) != 5 || gs[2] != (gate.SuiteEntry{Scenario: "old-incident", Severity: "low", KnownRegression: true}) ||
		gs[4].Scenario != "graph-only" {
		t.Fatalf("gate suite %+v", gs)
	}
	// A scenario the library did not confirm is not pinned, even if a version is known.
	odd := Suite(impact.Impact{Scenarios: []impact.Scenario{scenario("x", "low", "01a0d7b0-0000-7000-8000-000000000009", false, impact.Reasons{})}})
	if odd[0].Runnable() {
		t.Fatal("a scenario outside the library was pinned")
	}
	if empty := Suite(impact.Impact{}); empty == nil || len(empty) != 0 {
		t.Fatalf("%v", empty)
	}
}

func path(steps ...[2]string) []json.RawMessage {
	var out []json.RawMessage
	for _, s := range steps {
		b, _ := json.Marshal(map[string]any{"component": map[string]string{"kind": s[0], "key": s[1]}, "label": s[1]})
		out = append(out, b)
	}
	return out
}

func affected(kind, key, severity string, p []json.RawMessage) impact.Affected {
	return impact.Affected{Component: changes.Ref{Kind: kind, Key: key}, Severity: severity, Path: p}
}

func TestAComponentIsTestedThroughTheToolsOnItsPath(t *testing.T) {
	prompt := [2]string{"PROMPT", "support"}
	refund := [2]string{"TOOL", "refund_payment"}
	imp := impact.Impact{
		Scenarios: []impact.Scenario{
			scenario("refund", "high", "01a0d7b0-0000-7000-8000-000000000001", true,
				impact.Reasons{Graph: []impact.ScenarioGraphReason{via("TOOL", "refund_payment"), via("POLICY", "refund-limit"),
					via("RETRIEVAL_SOURCE", "refund-kb")}}),
			// It tests the email tool, but it cannot run: it tests nothing.
			{Name: "unconfirmed", Severity: "high", Reasons: impact.Reasons{Graph: []impact.ScenarioGraphReason{via("TOOL", "send_email")}}},
		},
		Graph: &impact.GraphSummary{Affected: []impact.Affected{
			affected("TOOL", "refund_payment", "critical", path(prompt, refund)),
			affected("SERVICE", "payments-api", "high", path(prompt, refund, [2]string{"HTTP_API", "payments"}, [2]string{"SERVICE", "payments-api"})),
			affected("TOOL", "send_email", "high", path(prompt, [2]string{"TOOL", "send_email"})),
			// A service that shares a key with a tested tool is not that tool.
			affected("SERVICE", "refund_payment", "high", path(prompt, [2]string{"SERVICE", "refund_payment"})),
			// The tool on the path is not tested: neither is what it reaches.
			affected("DATABASE", "mail-log", "high", path(prompt, [2]string{"TOOL", "send_email"}, [2]string{"DATABASE", "mail-log"})),
			// A scenario that checks a policy on the path does not exercise
			// what lies behind it; only a tool does.
			affected("DATABASE", "ledger", "high", path(prompt, [2]string{"POLICY", "refund-limit"}, [2]string{"DATABASE", "ledger"})),
			// A component a scenario tests itself is tested, tool or not.
			affected("RETRIEVAL_SOURCE", "refund-kb", "high", path(prompt, [2]string{"RETRIEVAL_SOURCE", "refund-kb"})),
		}},
		IrreversibleActions: []impact.Affected{
			affected("TOOL", "refund_payment", "critical", path(prompt, refund)),
			affected("TOOL", "delete_account", "critical", path(prompt, [2]string{"TOOL", "delete_account"})),
		},
		NewPrivileges: []impact.Privilege{
			{Tool: "delete_account", Change: "added", Risk: "ADMIN"},
			{Tool: "refund_payment", Change: "escalated", Risk: "WRITE_IRREVERSIBLE", From: "WRITE_REVERSIBLE"},
		},
	}
	gi := GateImpact(false, []string{"graph-service: UPSTREAM_ERROR"}, imp, Suite(imp))
	got := map[string]bool{}
	for _, c := range gi.Affected {
		got[c.Kind+":"+c.Key] = c.Tested
	}
	want := map[string]bool{"TOOL:refund_payment": true, "SERVICE:payments-api": true, "TOOL:send_email": false,
		"SERVICE:refund_payment": false, "DATABASE:mail-log": false, "DATABASE:ledger": false,
		"RETRIEVAL_SOURCE:refund-kb": true}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s tested %v, want %v", k, got[k], v)
		}
	}
	if gi.Affected[1].Severity != "high" || len(gi.Affected) != 7 {
		t.Fatalf("%+v", gi.Affected)
	}
	if len(gi.IrreversibleActions) != 2 || !gi.IrreversibleActions[0].Tested || gi.IrreversibleActions[1].Tested {
		t.Fatalf("irreversible %+v", gi.IrreversibleActions)
	}
	if gi.Complete || !slices.Equal(gi.Problems, []string{"graph-service: UPSTREAM_ERROR"}) {
		t.Fatalf("%+v", gi)
	}
	if !slices.Equal(gi.NewPrivileges, []string{
		"The candidate adds the tool delete_account with ADMIN risk.",
		"The tool refund_payment is escalated from WRITE_REVERSIBLE to WRITE_IRREVERSIBLE.",
	}) {
		t.Fatalf("%q", gi.NewPrivileges)
	}
	// Without the graph, nothing is known to be reached.
	none := GateImpact(true, nil, impact.Impact{}, nil)
	if !none.Complete || none.Problems == nil || len(none.Affected) != 0 || none.IrreversibleActions == nil || none.NewPrivileges == nil {
		t.Fatalf("%+v", none)
	}
}

func TestTheCostCountsTheJudgeAndBothSides(t *testing.T) {
	c := load(t)
	cost := CostOf(c.Run)
	if cost.JudgeUSD != 0 || cost.AgentUSD == nil || *cost.AgentUSD != 0.043+0.086 || cost.AgentCasesKnown != 16 ||
		cost.TotalUSD != 0.043+0.086 {
		t.Fatalf("%+v", cost)
	}
	var d RunDetail
	if got := CostOf(d); got.AgentUSD != nil || got.TotalUSD != 0 {
		t.Fatalf("%+v", got)
	}
	raw := `{"run": {"budget": {"spent_usd": 0.25}}, "summary": {"baseline": {"cost_usd": null, "cost_known": 0},
		"candidate": {"cost_usd": 0.5, "cost_known": 3}}}`
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		t.Fatal(err)
	}
	got := CostOf(d)
	if got.JudgeUSD != 0.25 || *got.AgentUSD != 0.5 || got.AgentCasesKnown != 3 || got.TotalUSD != 0.75 {
		t.Fatalf("%+v", got)
	}
}

func TestAFailingExpectationKeepsAFewObservations(t *testing.T) {
	var d CaseDetail
	raw := `{"comparison": {"scenario_name": "s", "expectations": [
		{"id": "a", "type": "state", "critical": true, "baseline": "PASS", "candidate": "FAIL"},
		{"id": "b", "type": "state", "critical": true, "baseline": "PASS", "candidate": "ERROR"},
		{"id": "c", "type": "semantic", "critical": false, "baseline": null, "candidate": "PASS"}]},
	  "results": {"baseline": [], "candidate": [
		{"status": "FAIL", "expectation": {"id": "a", "type": "state"}, "evidence": [
			{"detail": "1", "expected": 1, "actual": 2}, {"detail": "2"}, {"detail": "3"}, {"detail": "4"}, {"detail": "5"}]},
		{"status": "ERROR", "expectation": {"id": "b", "type": "state"}, "evidence": [{"detail": "boom", "ref": "step 3"}]},
		{"status": "PASS", "expectation": {"id": "c", "type": "semantic", "category": "groundedness"},
		 "evidence": [{"detail": "fine"}]}]}}`
	if err := json.Unmarshal([]byte(raw), &d); err != nil {
		t.Fatal(err)
	}
	got := expectations(d)
	if len(got) != 3 {
		t.Fatalf("%+v", got)
	}
	a, b, c := got[0], got[1], got[2]
	if len(a.Observed) != MaxObservations || a.Observed[0] != (gate.Observation{Detail: "1", Expected: 1.0, Actual: 2.0}) ||
		a.Observed[2].Detail != "3" {
		t.Fatalf("a: %+v", a.Observed)
	}
	if len(b.Observed) != 1 || b.Observed[0] != (gate.Observation{Detail: "boom", Ref: "step 3"}) || b.Candidate != "ERROR" {
		t.Fatalf("b: %+v", b)
	}
	if len(c.Observed) != 0 || c.Criterion != "groundedness" || c.Baseline != "" || c.Candidate != "PASS" {
		t.Fatalf("c: %+v", c)
	}
}
