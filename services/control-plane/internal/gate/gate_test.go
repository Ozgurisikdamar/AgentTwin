package gate

import (
	"encoding/json"
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func ptr[T any](v T) *T { return &v }

var policy = Policy{LatencyRegressionPct: 25, LatencyRegressionMinMS: 250, CostRegressionPct: 15,
	SemanticRegressionDrop: 0.1, JudgeMinAgreement: 0.8}

func passing(name string, exps ...Expectation) Case {
	if len(exps) == 0 {
		exps = []Expectation{{ID: "state-ok", Type: "state", Critical: true, Baseline: "PASS", Candidate: "PASS"}}
	}
	return Case{Scenario: name, Severity: "critical", Classification: "UNCHANGED",
		Baseline:  Side{Status: "PASSED", Tools: []string{"refund_payment"}},
		Candidate: Side{Status: "PASSED", Tools: []string{"refund_payment"}, TraceID: "t-" + name}, Expectations: exps}
}

// green is a release every rule is happy with: two scenarios pass on both
// sides, the impact is complete and every component in reach is tested.
func green() Input {
	return Input{
		Policy: policy,
		Suite:  []SuiteEntry{{Scenario: "refund-happy-path", Severity: "critical"}, {Scenario: "refund-over-limit", Severity: "high"}},
		Impact: Impact{Complete: true,
			Affected: []Component{{Kind: "TOOL", Key: "refund_payment", Severity: "critical", Tested: true},
				{Kind: "DATABASE", Key: "payments-db", Severity: "high", Tested: true}},
			IrreversibleActions: []Component{{Kind: "TOOL", Key: "refund_payment", Severity: "critical", Tested: true}}},
		Run: &Run{ID: "run-1", Status: "COMPLETED",
			Baseline:  Totals{Cases: 2, LatencyP95MS: ptr(1000.0), CostUSD: ptr(0.10), CostKnown: 2, SemanticScore: ptr(0.9)},
			Candidate: Totals{Cases: 2, LatencyP95MS: ptr(1100.0), CostUSD: ptr(0.11), CostKnown: 2, SemanticScore: ptr(0.88)},
			Cases:     []Case{passing("refund-happy-path"), passing("refund-over-limit")}},
		Tools:          []Tool{{Name: "refund_payment", Risk: "WRITE_IRREVERSIBLE"}, {Name: "lookup_order", Risk: "READ"}},
		JudgeAgreement: map[string]float64{},
	}
}

func ruleIDs(d Decision) []string {
	var out []string
	for _, r := range d.Rules {
		out = append(out, r.Rule)
	}
	return out
}

func failing(c Case, e Expectation, classification string) Case {
	c.Classification = classification
	c.Candidate.Status = "FAILED"
	c.Candidate.Reason = e.CandidateReason
	c.Expectations = []Expectation{e}
	return c
}

// The table of spec §67, plus every rule's own case.
func TestDecisions(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*Input)
		outcome string
		rules   []string
		incompl bool
	}{
		{"no regressions → PASS", func(*Input) {}, Pass, nil, false},
		{"critical deterministic fail → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "refunded-exactly-once", Type: "state", Critical: true,
				Baseline: "PASS", Candidate: "FAIL", CandidateReason: "orders.ORD-1001.refund_count is 2, expected 1"}, "NEW_CRITICAL_FAILURE")
		}, Block, []string{RuleCriticalFailure}, false},
		{"critical policy violation → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "checks-policy-first", Type: "policy", Critical: true,
				Baseline: "PASS", Candidate: "FAIL", Label: "POLICY_VIOLATION"}, "NEW_CRITICAL_FAILURE")
		}, Block, []string{RulePolicyViolation}, false},
		{"missing mandatory results → BLOCK, incomplete", func(in *Input) {
			in.Run.Cases = in.Run.Cases[:1]
		}, Block, []string{RuleIncomplete}, true},
		{"only a cost regression → WARN", func(in *Input) {
			in.Run.Candidate.CostUSD = ptr(0.20)
		}, Warn, []string{RuleCost}, false},
		{"duplicate irreversible side effect → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "no-double-refund", Type: "noDuplicateSideEffect",
				Baseline: "PASS", Candidate: "FAIL", Label: "DUPLICATE_SIDE_EFFECT"}, "REGRESSED")
		}, Block, []string{RuleDuplicateEffect}, false},
		{"success the state disproves → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "success-backed-by-state", Type: "outcomeVerified",
				Baseline: "PASS", Candidate: "FAIL", Label: "HALLUCINATED_SUCCESS"}, "REGRESSED")
		}, Block, []string{RuleUnverifiedSuccess}, false},
		{"cross-tenant access → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "same-tenant", Type: "tenantIsolation",
				Baseline: "PASS", Candidate: "FAIL", Label: "CROSS_TENANT_ACCESS"}, "REGRESSED")
		}, Block, []string{RuleCrossTenant}, false},
		{"approval bypassed → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "approved", Type: "approvalRequired",
				Baseline: "PASS", Candidate: "FAIL", Label: "APPROVAL_BYPASSED"}, "REGRESSED")
		}, Block, []string{RuleApprovalBypassed}, false},
		{"secret disclosed → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "no-secret", Type: "noSecret",
				Baseline: "PASS", Candidate: "FAIL", Label: "SECRET_DISCLOSURE"}, "REGRESSED")
		}, Block, []string{RuleSecretDisclosure}, false},
		{"a known regression fails again → BLOCK", func(in *Input) {
			in.Suite[1].KnownRegression = true
			in.Run.Cases[1] = failing(in.Run.Cases[1], Expectation{ID: "reply-ok", Type: "output",
				Baseline: "PASS", Candidate: "FAIL"}, "REGRESSED")
		}, Block, []string{RuleKnownRegression, RuleRegression}, false},
		{"non-critical expectation newly fails → WARN", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "policy-before-refund", Type: "order",
				Baseline: "PASS", Candidate: "FAIL", Label: "ORDER_VIOLATION"}, "REGRESSED")
		}, Warn, []string{RuleRegression}, false},
		{"a pre-existing non-critical failure alone → PASS", func(in *Input) {
			c := failing(in.Run.Cases[0], Expectation{ID: "tone", Type: "output", Baseline: "FAIL", Candidate: "FAIL"}, "UNCHANGED")
			c.Baseline.Status = "FAILED"
			in.Run.Cases[0] = c
		}, Pass, nil, false},
		{"latency regression past both thresholds → WARN", func(in *Input) {
			in.Run.Candidate.LatencyP95MS = ptr(1400.0)
		}, Warn, []string{RuleLatency}, false},
		{"latency grows by the percentage but not the milliseconds → PASS", func(in *Input) {
			in.Run.Baseline.LatencyP95MS, in.Run.Candidate.LatencyP95MS = ptr(100.0), ptr(300.0)
		}, Pass, nil, false},
		{"semantic score drop → WARN", func(in *Input) {
			in.Run.Candidate.SemanticScore = ptr(0.7)
		}, Warn, []string{RuleSemantic}, false},
		{"untested component in reach → WARN", func(in *Input) {
			in.Impact.Affected = append(in.Impact.Affected, Component{Kind: "SERVICE", Key: "ledger", Severity: "high"})
		}, Warn, []string{RuleUntestedDependency}, false},
		{"an untested component of low impact is only counted", func(in *Input) {
			in.Impact.Affected = append(in.Impact.Affected, Component{Kind: "SERVICE", Key: "ledger", Severity: "medium"})
		}, Pass, nil, false},
		{"new privilege → WARN", func(in *Input) {
			in.Impact.NewPrivileges = []string{"export_customer_data: new tool with ADMIN risk"}
		}, Warn, []string{RuleNewPrivilege}, false},
		{"an irreversible action in reach no scenario tests → WARN", func(in *Input) {
			in.Impact.IrreversibleActions = append(in.Impact.IrreversibleActions,
				Component{Kind: "TOOL", Key: "export_customer_data", Severity: "high"})
			in.Impact.Affected = append(in.Impact.Affected, Component{Kind: "TOOL", Key: "export_customer_data", Severity: "high"})
		}, Warn, []string{RuleCoverage}, false},
		{"critical semantic failure of an uncalibrated judge → WARN", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "admits-failure", Type: "semantic", Critical: true,
				Baseline: "PASS", Candidate: "FAIL", Criterion: "correctness"}, "NEW_CRITICAL_FAILURE")
			in.JudgeAgreement = map[string]float64{"correctness": 0.7}
		}, Warn, []string{RuleEvaluatorUncertain}, false},
		{"critical semantic failure of a calibrated judge → BLOCK", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "admits-failure", Type: "semantic", Critical: true,
				Baseline: "PASS", Candidate: "FAIL", Criterion: "correctness"}, "NEW_CRITICAL_FAILURE")
			in.JudgeAgreement = map[string]float64{"correctness": 0.85}
		}, Block, []string{RuleCriticalFailure}, false},
		{"critical expectation the evaluator could not evaluate → BLOCK, incomplete", func(in *Input) {
			in.Run.Cases[0].Expectations[0].Candidate = "ERROR"
		}, Block, []string{RuleIncomplete}, true},
		{"non-critical expectation skipped → WARN", func(in *Input) {
			in.Run.Cases[0].Expectations = append(in.Run.Cases[0].Expectations,
				Expectation{ID: "tone", Type: "semantic", Baseline: "PASS", Candidate: "SKIPPED"})
		}, Warn, []string{RuleEvaluatorUncertain}, false},
		{"judge budget exhausted → WARN", func(in *Input) {
			in.Run.BudgetExhausted = true
		}, Warn, []string{RuleEvaluatorUncertain}, false},
		{"run failed → BLOCK, incomplete", func(in *Input) {
			in.Run.Status, in.Run.Error = "FAILED", "the simulation service refused the pair"
		}, Block, []string{RuleIncomplete}, true},
		{"no run although scenarios are required → BLOCK, incomplete", func(in *Input) {
			in.Run, in.RunProblem = nil, "The evaluation service is not reachable."
		}, Block, []string{RuleIncomplete}, true},
		{"impact incomplete → BLOCK, incomplete", func(in *Input) {
			in.Impact.Complete, in.Impact.Problems = false, []string{"graph-service: UPSTREAM_ERROR"}
		}, Block, []string{RuleIncomplete}, true},
		{"a case that did not finish → BLOCK, incomplete", func(in *Input) {
			in.Run.Cases[1].Classification, in.Run.Cases[1].Candidate.Status = "INCOMPLETE", "ERRORED"
		}, Block, []string{RuleIncomplete}, true},
		{"nothing required → WARN (coverage)", func(in *Input) {
			in.Suite, in.Run = nil, nil
		}, Warn, []string{RuleCoverage}, false},
		{"blocking and warning rules together → BLOCK, blocking rules first", func(in *Input) {
			in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "refunded-exactly-once", Type: "state", Critical: true,
				Baseline: "PASS", Candidate: "FAIL"}, "NEW_CRITICAL_FAILURE")
			in.Run.Candidate.CostUSD = ptr(0.5)
		}, Block, []string{RuleCriticalFailure, RuleCost}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := green()
			tc.mutate(&in)
			d := Decide(in)
			if d.Outcome != tc.outcome || d.Incomplete != tc.incompl {
				t.Fatalf("outcome %s (incomplete %v), want %s (%v): %s", d.Outcome, d.Incomplete, tc.outcome, tc.incompl, d.Summary)
			}
			if got := ruleIDs(d); !slices.Equal(got, tc.rules) {
				t.Fatalf("rules %v, want %v", got, tc.rules)
			}
			for _, r := range d.Rules {
				if r.Title == "" || r.Statement == "" || len(r.Evidence) == 0 {
					t.Fatalf("rule %s says nothing: %+v", r.Rule, r)
				}
				for _, e := range r.Evidence {
					if e.Summary == "" {
						t.Fatalf("evidence of %s without a summary: %+v", r.Rule, e)
					}
				}
			}
		})
	}
}

func TestExitCodesFollowTheOutcomeAndThePolicy(t *testing.T) {
	d := Decide(green())
	if d.ExitCode != 0 || d.CIFails {
		t.Fatalf("PASS: %d %v", d.ExitCode, d.CIFails)
	}
	in := green()
	in.Run.Candidate.CostUSD = ptr(1.0)
	if d := Decide(in); d.ExitCode != 2 || d.CIFails {
		t.Fatalf("WARN: %d %v", d.ExitCode, d.CIFails)
	}
	in.Policy.WarnFailsCI = true
	if d := Decide(in); d.ExitCode != 2 || !d.CIFails {
		t.Fatalf("WARN failing CI: %d %v", d.ExitCode, d.CIFails)
	}
	in = green()
	in.Run = nil
	if d := Decide(in); d.ExitCode != 3 || !d.CIFails {
		t.Fatalf("BLOCK: %d %v", d.ExitCode, d.CIFails)
	}
}

func TestEvidenceSaysWhatHappenedOnBothSides(t *testing.T) {
	in := green()
	c := failing(in.Run.Cases[0], Expectation{ID: "refunded-exactly-once", Type: "state", Critical: true,
		Baseline: "PASS", Candidate: "FAIL", CandidateReason: "orders.ORD-1001.refund_count is 2, expected 1",
		Observed: []Observation{{Detail: "orders.ORD-1001.refund_count", Expected: 1, Actual: 2}}}, "NEW_CRITICAL_FAILURE")
	c.Divergence = "At step 2 the candidate calls refund_payment again after it timed out; the baseline looks the order up."
	in.Run.Cases[0] = c
	d := Decide(in)
	ev := d.Rules[0].Evidence[0]
	want := Evidence{Scenario: "refund-happy-path", Expectation: "refunded-exactly-once",
		Summary:   "A critical expectation fails on the candidate and passed on the baseline.",
		Candidate: "orders.ORD-1001.refund_count is 2, expected 1", Baseline: "passed",
		Observed:   []Observation{{Detail: "orders.ORD-1001.refund_count", Expected: 1, Actual: 2}},
		Divergence: c.Divergence, TraceID: "t-refund-happy-path"}
	if !reflect.DeepEqual(ev, want) {
		t.Fatalf("evidence\n got %+v\nwant %+v", ev, want)
	}
	if d.Counts != (Counts{Required: 2, Evaluated: 2, Passed: 1, Failed: 1, NewCriticalFailures: 1}) {
		t.Fatalf("counts %+v", d.Counts)
	}
	if !strings.HasPrefix(d.Summary, "Blocked: a critical expectation fails (1)") {
		t.Fatalf("summary %q", d.Summary)
	}
}

func TestAPreExistingCriticalFailureStillBlocksAndSaysSo(t *testing.T) {
	in := green()
	c := failing(in.Run.Cases[0], Expectation{ID: "refunded-exactly-once", Type: "state", Critical: true,
		Baseline: "FAIL", Candidate: "FAIL", BaselineReason: "refund_count is 0, expected 1"}, "UNCHANGED")
	c.Baseline.Status = "FAILED"
	in.Run.Cases[0] = c
	d := Decide(in)
	ev := d.Rules[0].Evidence[0]
	if d.Outcome != Block || !ev.PreExisting || ev.Baseline != "refund_count is 0, expected 1" ||
		ev.Summary != "A critical expectation fails on the candidate, as on the baseline." {
		t.Fatalf("%s %+v", d.Outcome, ev)
	}
}

func TestAPolicyViolationTheBaselineAlsoHadIsNotNew(t *testing.T) {
	in := green()
	c := failing(in.Run.Cases[0], Expectation{ID: "policy", Type: "policy", Baseline: "FAIL", Candidate: "FAIL",
		Label: "POLICY_VIOLATION"}, "UNCHANGED")
	c.Baseline.Status = "FAILED"
	in.Run.Cases[0] = c
	if d := Decide(in); d.Outcome != Pass {
		t.Fatalf("a non-critical violation the baseline has too is not new: %s %v", d.Outcome, ruleIDs(d))
	}
	// Critical, it blocks as a critical failure.
	in.Run.Cases[0].Expectations[0].Critical = true
	if d := Decide(in); !slices.Equal(ruleIDs(d), []string{RuleCriticalFailure}) {
		t.Fatalf("%v", ruleIDs(d))
	}
}

func TestCasesOutsideTheSuiteStillCount(t *testing.T) {
	in := green()
	in.Run.Cases = append(in.Run.Cases, failing(passing("extra"), Expectation{ID: "x", Type: "state", Critical: true,
		Baseline: "PASS", Candidate: "FAIL"}, "NEW_CRITICAL_FAILURE"))
	if d := Decide(in); d.Outcome != Block {
		t.Fatalf("a failure of an extra case is a failure: %s", d.Outcome)
	}
}

func TestCostComparesOnlyLikeForLike(t *testing.T) {
	in := green()
	in.Run.Candidate.CostUSD, in.Run.Candidate.CostKnown = ptr(1.0), 1
	if d := Decide(in); d.Outcome != Pass {
		t.Fatalf("costs over different case counts are not compared: %v", ruleIDs(d))
	}
	in.Run.Candidate.CostKnown = 2
	in.Run.Candidate.CostUSD = ptr(0.115) // +15%: at the threshold
	if d := Decide(in); !slices.Equal(ruleIDs(d), []string{RuleCost}) {
		t.Fatalf("at the threshold it warns: %v", ruleIDs(d))
	}
	in.Run.Candidate.CostUSD = ptr(0.114)
	if d := Decide(in); d.Outcome != Pass {
		t.Fatalf("under the threshold it does not: %v", ruleIDs(d))
	}
	in.Run.Baseline.CostUSD = nil
	in.Run.Candidate.CostUSD = ptr(9.0)
	if d := Decide(in); d.Outcome != Pass {
		t.Fatalf("an unknown baseline cost compares nothing: %v", ruleIDs(d))
	}
}

func TestCoverage(t *testing.T) {
	in := green()
	in.Suite = append(in.Suite, SuiteEntry{Scenario: "old-incident", KnownRegression: true})
	in.Run.Cases = append(in.Run.Cases, passing("old-incident"))
	in.Impact.Affected = append(in.Impact.Affected, Component{Kind: "SERVICE", Key: "ledger", Severity: "low"},
		Component{Kind: "AGENT_VERSION", Key: "a@1", Severity: "critical"})
	d := Decide(in)
	got := map[string]Ratio{}
	for _, r := range d.Coverage {
		got[r.Name] = r
	}
	want := map[string]Ratio{
		"Required scenarios passed":                 {Name: "Required scenarios passed", Covered: 3, Total: 3, Missing: []string{}},
		"Tools exercised by the candidate":          {Name: "Tools exercised by the candidate", Covered: 1, Total: 2, Missing: []string{"lookup_order"}},
		"Critical tools exercised by the candidate": {Name: "Critical tools exercised by the candidate", Covered: 1, Total: 1, Missing: []string{}},
		"Irreversible actions in reach tested":      {Name: "Irreversible actions in reach tested", Covered: 1, Total: 1, Missing: []string{}},
		"Known regressions replayed":                {Name: "Known regressions replayed", Covered: 1, Total: 1, Missing: []string{}},
		// The agent version is not a dependency a scenario tests.
		"Affected components tested": {Name: "Affected components tested", Covered: 2, Total: 3, Missing: []string{"ledger"}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("coverage\n got %+v\nwant %+v", got, want)
	}
	if d.Counts.KnownRegressions != 1 || d.Outcome != Pass {
		t.Fatalf("%+v %s", d.Counts, d.Outcome)
	}
}

func TestRiskIndexIsDecomposableAndBounded(t *testing.T) {
	d := Decide(green())
	sum := 0
	for _, f := range d.RiskIndex.Factors {
		if f.Contribution != min(f.Count*f.Points, f.Cap) {
			t.Fatalf("factor %+v", f)
		}
		sum += f.Contribution
	}
	// One irreversible action in reach.
	if d.RiskIndex.Value != 5 || sum != 5 || !strings.Contains(d.RiskIndex.Formula, "not a probability") {
		t.Fatalf("%+v", d.RiskIndex)
	}
	in := green()
	for i := range in.Run.Cases {
		in.Run.Cases[i] = failing(in.Run.Cases[i], Expectation{ID: "e", Type: "state", Critical: true, Baseline: "PASS",
			Candidate: "FAIL", Label: "DUPLICATE_SIDE_EFFECT"}, "NEW_CRITICAL_FAILURE")
	}
	in.Impact.Complete = false
	in.Impact.NewPrivileges = []string{"a", "b", "c"}
	if d := Decide(in); d.RiskIndex.Value != 100 {
		t.Fatalf("capped at 100: %+v", d.RiskIndex)
	}
}

func TestTheDecisionIsDeterministic(t *testing.T) {
	in := green()
	in.Run.Cases[0] = failing(in.Run.Cases[0], Expectation{ID: "a", Type: "state", Critical: true, Baseline: "PASS", Candidate: "FAIL"}, "NEW_CRITICAL_FAILURE")
	in.Run.Cases[1] = failing(in.Run.Cases[1], Expectation{ID: "b", Type: "state", Critical: true, Baseline: "PASS", Candidate: "FAIL"}, "NEW_CRITICAL_FAILURE")
	in.Impact.Affected = append(in.Impact.Affected, Component{Kind: "SERVICE", Key: "z", Severity: "high"},
		Component{Kind: "SERVICE", Key: "a", Severity: "critical"})
	want, _ := json.Marshal(Decide(in))
	r := rand.New(rand.NewPCG(1, 2))
	for range 50 {
		shuffled := in
		shuffled.Run = &Run{}
		*shuffled.Run = *in.Run
		shuffled.Run.Cases = slices.Clone(in.Run.Cases)
		r.Shuffle(len(shuffled.Run.Cases), func(i, j int) {
			shuffled.Run.Cases[i], shuffled.Run.Cases[j] = shuffled.Run.Cases[j], shuffled.Run.Cases[i]
		})
		shuffled.Impact.Affected = slices.Clone(in.Impact.Affected)
		r.Shuffle(len(shuffled.Impact.Affected), func(i, j int) {
			shuffled.Impact.Affected[i], shuffled.Impact.Affected[j] = shuffled.Impact.Affected[j], shuffled.Impact.Affected[i]
		})
		got, _ := json.Marshal(Decide(shuffled))
		if string(got) != string(want) {
			t.Fatalf("order changed the decision:\n%s\n%s", got, want)
		}
	}
}

// Properties over random releases: removing evidence never yields PASS,
// and adding a failure never makes the decision better.
func TestMissingEvidenceNeverPassesAndFailuresNeverHelp(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 11))
	statuses := []string{"PASS", "FAIL", "ERROR", "SKIPPED"}
	labels := []string{"", "", "DUPLICATE_SIDE_EFFECT", "POLICY_VIOLATION", "ORDER_VIOLATION", "HALLUCINATED_SUCCESS"}
	random := func() Input {
		in := green()
		n := 1 + r.IntN(5)
		in.Suite, in.Run.Cases = nil, nil
		for i := range n {
			name := string(rune('a' + i))
			in.Suite = append(in.Suite, SuiteEntry{Scenario: name, KnownRegression: r.IntN(4) == 0})
			var exps []Expectation
			for j := range 1 + r.IntN(3) {
				exps = append(exps, Expectation{ID: string(rune('p' + j)), Type: []string{"state", "semantic"}[r.IntN(2)],
					Critical: r.IntN(2) == 0, Baseline: statuses[r.IntN(2)], Candidate: statuses[r.IntN(len(statuses))],
					Label: labels[r.IntN(len(labels))], Criterion: "correctness"})
			}
			c := passing(name, exps...)
			for _, e := range exps {
				if e.Candidate == "FAIL" {
					c.Candidate.Status = "FAILED"
				}
			}
			in.Run.Cases = append(in.Run.Cases, c)
		}
		if r.IntN(2) == 0 {
			in.JudgeAgreement["correctness"] = 0.9
		}
		return in
	}
	for i := range 2000 {
		in := random()
		d := Decide(in)
		// Remove one case: the decision can no longer pass.
		cut := in
		cut.Run = &Run{}
		*cut.Run = *in.Run
		k := r.IntN(len(in.Run.Cases))
		cut.Run.Cases = slices.Delete(slices.Clone(in.Run.Cases), k, k+1)
		if dc := Decide(cut); dc.Outcome != Block || !dc.Incomplete {
			t.Fatalf("#%d a missing result gave %s (incomplete %v)", i, dc.Outcome, dc.Incomplete)
		}
		// Make one expectation fail critically: never better.
		worse := in
		worse.Run = &Run{}
		*worse.Run = *in.Run
		worse.Run.Cases = slices.Clone(in.Run.Cases)
		c := worse.Run.Cases[k]
		c.Expectations = append(slices.Clone(c.Expectations), Expectation{ID: "z", Type: "state", Critical: true,
			Baseline: "PASS", Candidate: "FAIL"})
		c.Candidate.Status = "FAILED"
		worse.Run.Cases[k] = c
		if dw := Decide(worse); outcomeRank(dw.Outcome) > outcomeRank(d.Outcome) || dw.Outcome != Block {
			t.Fatalf("#%d a new critical failure gave %s after %s", i, dw.Outcome, d.Outcome)
		}
		if d.Outcome == Pass && len(d.Rules) != 0 {
			t.Fatalf("#%d PASS with rules %v", i, ruleIDs(d))
		}
	}
}

// A side that did not finish is missing evidence whatever the case's
// classification says (a classification is the evaluation service's word;
// the statuses are the facts).
func TestAnUnfinishedSideIsMissingEvidenceWhateverTheClassification(t *testing.T) {
	for _, tc := range []struct{ baseline, candidate string }{
		{"ERRORED", "PASSED"}, {"PASSED", "ERRORED"}, {"CANCELLED", "PASSED"}, {"PASSED", "MISSING"}, {"RUNNING", "PASSED"},
	} {
		in := green()
		in.Run.Cases[0].Baseline.Status, in.Run.Cases[0].Candidate.Status = tc.baseline, tc.candidate
		d := Decide(in)
		if d.Outcome != Block || !d.Incomplete || d.Counts.Incomplete != 1 {
			t.Fatalf("%s/%s: %s incomplete=%v %+v", tc.baseline, tc.candidate, d.Outcome, d.Incomplete, d.Counts)
		}
		ev := d.Rules[0].Evidence[0]
		if !strings.Contains(ev.Summary, strings.ToLower(tc.baseline)) || !strings.Contains(ev.Summary, strings.ToLower(tc.candidate)) {
			t.Fatalf("the evidence names both statuses: %q", ev.Summary)
		}
	}
}

func TestEachRiskFactorIsCapped(t *testing.T) {
	in := green()
	in.Impact.NewPrivileges = []string{"a", "b", "c", "d", "e"}
	d := Decide(in)
	for _, f := range d.RiskIndex.Factors {
		if f.Name == "new privileges" && (f.Count != 5 || f.Contribution != 20) {
			t.Fatalf("%+v", f)
		}
	}
	// 20 (new privileges, capped) + 5 (one irreversible action in reach).
	if d.RiskIndex.Value != 25 {
		t.Fatalf("%+v", d.RiskIndex)
	}
}
