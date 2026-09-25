// Package release turns what a release evaluation gathered into the gate's
// input (spec §28): the suite pinned from the change's impact, what the
// change reaches and whether the suite tests it, and the evaluation
// service's answer (the run, every compared case and the judge's
// calibrations) as evidence. It is pure; the control plane fetches, this
// package maps, and package gate decides.
package release

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/gate"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/impact"
)

// ---------------------------------------------------------------- suite

// Entry is a scenario of a release's suite, pinned to the version the
// impact selected. ScenarioVersionID is empty for a scenario the graph
// links but the scenario library could not confirm: it is required, and
// cannot run, so its result is missing evidence.
type Entry struct {
	ScenarioVersionID string         `json:"scenario_version_id,omitempty"`
	ScenarioName      string         `json:"scenario_name"`
	Severity          string         `json:"severity"`
	Mandatory         bool           `json:"mandatory"`
	KnownRegression   bool           `json:"known_regression"`
	Reasons           impact.Reasons `json:"reasons"`
	Why               []string       `json:"why"`
}

// Runnable reports whether the entry names a version the simulation service can run.
func (e Entry) Runnable() bool { return e.ScenarioVersionID != "" }

// Suite is every scenario the impact requires, in its order (severity, then
// name), each pinned to the version the impact selected. A scenario is
// mandatory when it is critical, always run by the gate policy, or a known
// production regression.
func Suite(imp impact.Impact) []Entry {
	out := make([]Entry, 0, len(imp.Scenarios))
	for _, sc := range imp.Scenarios {
		e := Entry{ScenarioName: sc.Name, Severity: sc.Severity, KnownRegression: sc.Reasons.KnownRegression,
			Reasons: sc.Reasons, Why: sc.Why}
		if sc.InLibrary {
			e.ScenarioVersionID = strings.ToLower(sc.LatestVersionID)
		}
		e.Mandatory = sc.Severity == "critical" || len(sc.Reasons.AlwaysRunTags) > 0 || e.KnownRegression
		if e.Why == nil {
			e.Why = []string{}
		}
		out = append(out, e)
	}
	return out
}

// GateSuite is the suite as the gate reads it: every required scenario,
// runnable or not.
func GateSuite(suite []Entry) []gate.SuiteEntry {
	out := make([]gate.SuiteEntry, 0, len(suite))
	for _, e := range suite {
		out = append(out, gate.SuiteEntry{Scenario: e.ScenarioName, Severity: e.Severity, KnownRegression: e.KnownRegression})
	}
	return out
}

// ---------------------------------------------------------------- impact

// step is one hop of a graph path, as far as testing is concerned.
type step struct {
	Component struct {
		Kind string `json:"kind"`
		Key  string `json:"key"`
	} `json:"component"`
}

func refID(kind, key string) string { return kind + ":" + key }

// testedBy is what the runnable scenarios of the suite test: the components
// the graph links them through.
func testedBy(suite []Entry) map[string]bool {
	out := map[string]bool{}
	for _, e := range suite {
		if !e.Runnable() {
			continue
		}
		for _, r := range e.Reasons.Graph {
			out[refID(r.Via.Kind, r.Via.Key)] = true
		}
	}
	return out
}

// tested reports whether the suite tests a reached component: a scenario
// tests it, or tests a tool on its path from the change (a database behind a
// tested tool is exercised by that tool's scenarios).
func tested(a impact.Affected, by map[string]bool) bool {
	if by[refID(a.Component.Kind, a.Component.Key)] {
		return true
	}
	for _, raw := range a.Path {
		var s step
		if json.Unmarshal(raw, &s) == nil && s.Component.Kind == "TOOL" && by[refID(s.Component.Kind, s.Component.Key)] {
			return true
		}
	}
	return false
}

func component(a impact.Affected, by map[string]bool) gate.Component {
	return gate.Component{Kind: a.Component.Kind, Key: a.Component.Key, Severity: a.Severity, Tested: tested(a, by)}
}

// GateImpact is the impact as the gate reads it. Problems are the services
// the impact could not ask; complete is false when one did not answer or an
// answer was cut short. The affected components are those the impact lists
// (the highest-scored first, at most impact.MaxAffectedShown).
func GateImpact(complete bool, problems []string, imp impact.Impact, suite []Entry) gate.Impact {
	by := testedBy(suite)
	out := gate.Impact{Complete: complete, Problems: append([]string{}, problems...), Affected: []gate.Component{},
		IrreversibleActions: []gate.Component{}, NewPrivileges: []string{}}
	if imp.Graph != nil {
		for _, a := range imp.Graph.Affected {
			out.Affected = append(out.Affected, component(a, by))
		}
	}
	for _, a := range imp.IrreversibleActions {
		out.IrreversibleActions = append(out.IrreversibleActions, component(a, by))
	}
	for _, p := range imp.NewPrivileges {
		out.NewPrivileges = append(out.NewPrivileges, Privilege(p))
	}
	return out
}

// Privilege says a new privilege in a sentence.
func Privilege(p impact.Privilege) string {
	if p.Change == "escalated" {
		return fmt.Sprintf("The tool %s is escalated from %s to %s.", p.Tool, p.From, p.Risk)
	}
	return fmt.Sprintf("The candidate adds the tool %s with %s risk.", p.Tool, p.Risk)
}

// ---------------------------------------------------------------- the evaluation service's answer

// JudgeIdentity is the judge a run was graded by.
type JudgeIdentity struct {
	Provider     string `json:"provider"`
	Model        string `json:"model"`
	Kind         string `json:"kind"`
	PromptSHA256 string `json:"prompt_sha256"`
}

// SideTotals is one side's totals over a run.
type SideTotals struct {
	Cases         int      `json:"cases"`
	LatencyP95MS  *float64 `json:"latency_ms_p95"`
	CostUSD       *float64 `json:"cost_usd"`
	CostKnown     int      `json:"cost_known"`
	SemanticScore *float64 `json:"semantic_score"`
}

// SideSummary is one side of a compared case.
type SideSummary struct {
	Status  string   `json:"status"`
	Reason  *string  `json:"reason"`
	TraceID *string  `json:"trace_id"`
	Labels  []string `json:"labels"`
	Tools   []string `json:"tools"`
}

// CaseSummary is a compared case as a run lists it.
type CaseSummary struct {
	ScenarioName   string      `json:"scenario_name"`
	Severity       string      `json:"severity"`
	Classification string      `json:"classification"`
	Reason         string      `json:"reason"`
	Baseline       SideSummary `json:"baseline"`
	Candidate      SideSummary `json:"candidate"`
}

// RunDetail is the evaluation service's answer for a run (the parts the
// gate reads).
type RunDetail struct {
	Run struct {
		ID                  string         `json:"id"`
		ReleaseEvaluationID *string        `json:"release_evaluation_id"`
		Status              string         `json:"status"`
		Error               *string        `json:"error"`
		Judge               *JudgeIdentity `json:"judge"`
		Budget              *struct {
			SpentUSD  float64 `json:"spent_usd"`
			Exhausted bool    `json:"exhausted"`
		} `json:"budget"`
	} `json:"run"`
	Summary *struct {
		Baseline  SideTotals `json:"baseline"`
		Candidate SideTotals `json:"candidate"`
	} `json:"summary"`
	Cases []CaseSummary `json:"cases"`
}

// ExpectationChange is one expectation of a compared case on both sides.
type ExpectationChange struct {
	ID              string  `json:"id"`
	Type            string  `json:"type"`
	Critical        bool    `json:"critical"`
	Baseline        *string `json:"baseline"`
	Candidate       *string `json:"candidate"`
	Label           string  `json:"label"`
	CandidateReason string  `json:"candidate_reason"`
	BaselineReason  string  `json:"baseline_reason"`
}

// ExpectationResult is one side's result of an expectation.
type ExpectationResult struct {
	Status      string `json:"status"`
	Expectation struct {
		ID       string `json:"id"`
		Type     string `json:"type"`
		Category string `json:"category"`
	} `json:"expectation"`
	Evidence []struct {
		Detail   string `json:"detail"`
		Ref      string `json:"ref"`
		Expected any    `json:"expected"`
		Actual   any    `json:"actual"`
	} `json:"evidence"`
}

// CaseDetail is the evaluation service's answer for one compared case.
type CaseDetail struct {
	Comparison struct {
		ScenarioName string              `json:"scenario_name"`
		Expectations []ExpectationChange `json:"expectations"`
		Divergence   *struct {
			Summary string  `json:"summary"`
			Impact  *string `json:"impact"`
		} `json:"divergence"`
	} `json:"comparison"`
	Results struct {
		Baseline  []ExpectationResult `json:"baseline"`
		Candidate []ExpectationResult `json:"candidate"`
	} `json:"results"`
}

// criteria are the fixed judge criteria; any other category is graded
// against the expectation's own rubric.
var criteria = map[string]bool{"task_completion": true, "intent_fidelity": true, "relevance": true, "correctness": true,
	"groundedness": true, "policy_adherence": true, "escalation_appropriateness": true}

// Criterion is the judge criterion a semantic expectation's category is graded as.
func Criterion(category string) string {
	if criteria[category] {
		return category
	}
	return "rubric"
}

// MaxObservations bounds the values kept per failing expectation.
const MaxObservations = 3

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func side(s SideSummary) gate.Side {
	return gate.Side{Status: s.Status, Reason: deref(s.Reason), TraceID: deref(s.TraceID),
		Labels: append([]string{}, s.Labels...), Tools: append([]string{}, s.Tools...)}
}

func totals(t SideTotals) gate.Totals {
	return gate.Totals{Cases: t.Cases, LatencyP95MS: t.LatencyP95MS, CostUSD: t.CostUSD, CostKnown: t.CostKnown,
		SemanticScore: t.SemanticScore}
}

// expectations are a case's expectations with the candidate's category and
// observations: the criterion a semantic one was graded as, and what the
// evaluator saw when the candidate failed or could not be evaluated.
func expectations(d CaseDetail) []gate.Expectation {
	results := map[string]ExpectationResult{}
	for _, r := range d.Results.Candidate {
		results[r.Expectation.ID] = r
	}
	out := make([]gate.Expectation, 0, len(d.Comparison.Expectations))
	for _, x := range d.Comparison.Expectations {
		e := gate.Expectation{ID: x.ID, Type: x.Type, Critical: x.Critical, Baseline: deref(x.Baseline),
			Candidate: deref(x.Candidate), Label: x.Label, CandidateReason: x.CandidateReason, BaselineReason: x.BaselineReason}
		r, ok := results[x.ID]
		if e.Semantic() {
			e.Criterion = Criterion(r.Expectation.Category)
		}
		if ok && e.Candidate != "PASS" {
			for _, ev := range r.Evidence {
				if len(e.Observed) == MaxObservations {
					break
				}
				e.Observed = append(e.Observed, gate.Observation{Detail: ev.Detail, Ref: ev.Ref, Expected: ev.Expected,
					Actual: ev.Actual})
			}
		}
		out = append(out, e)
	}
	return out
}

// GateRun is the run as the gate reads it: every listed case, with its
// expectations from its detail (a case without a detail has none; its
// sides' statuses still count).
func GateRun(d RunDetail, details map[string]CaseDetail) *gate.Run {
	run := &gate.Run{ID: d.Run.ID, Status: d.Run.Status, Error: deref(d.Run.Error), Cases: []gate.Case{}}
	if d.Run.Budget != nil {
		run.BudgetExhausted = d.Run.Budget.Exhausted
	}
	if d.Summary != nil {
		run.Baseline, run.Candidate = totals(d.Summary.Baseline), totals(d.Summary.Candidate)
	}
	for _, c := range d.Cases {
		gc := gate.Case{Scenario: c.ScenarioName, Severity: c.Severity, Classification: c.Classification, Reason: c.Reason,
			Baseline: side(c.Baseline), Candidate: side(c.Candidate), Expectations: []gate.Expectation{}}
		if det, ok := details[c.ScenarioName]; ok {
			gc.Expectations = expectations(det)
			if dv := det.Comparison.Divergence; dv != nil {
				gc.Divergence, gc.DivergenceImpact = dv.Summary, deref(dv.Impact)
			}
		}
		run.Cases = append(run.Cases, gc)
	}
	return run
}

// ---------------------------------------------------------------- the judge

// JudgeDescription is the evaluation service's description of its judge
// and the latest calibration of each criterion.
type JudgeDescription struct {
	Criteria []struct {
		Criterion   string `json:"criterion"`
		Calibrated  bool   `json:"calibrated"`
		Calibration *struct {
			Judge   *JudgeIdentity `json:"judge"`
			Metrics *struct {
				Accuracy float64 `json:"accuracy"`
			} `json:"metrics"`
		} `json:"calibration"`
	} `json:"criteria"`
}

// JudgeAgreement is each criterion's calibration agreement for the judge
// that graded the run: only calibrations the evaluation service counts as
// calibrated, of that same judge (provider, model and prompt). A criterion
// without one is absent — its verdicts cannot block.
func JudgeAgreement(graded *JudgeIdentity, desc JudgeDescription) map[string]float64 {
	out := map[string]float64{}
	if graded == nil {
		return out
	}
	for _, c := range desc.Criteria {
		cal := c.Calibration
		if !c.Calibrated || cal == nil || cal.Judge == nil || cal.Metrics == nil {
			continue
		}
		j := cal.Judge
		if j.Provider != graded.Provider || j.Model != graded.Model || j.PromptSHA256 != graded.PromptSHA256 {
			continue
		}
		out[c.Criterion] = cal.Metrics.Accuracy
	}
	return out
}

// ---------------------------------------------------------------- cost

// Cost is what evaluating the release spent (spec §49): the judge's calls
// and the agent's model usage on both sides, over the cases whose usage is
// known.
type Cost struct {
	JudgeUSD        float64  `json:"judge_usd"`
	AgentUSD        *float64 `json:"agent_usd"`
	AgentCasesKnown int      `json:"agent_cases_known"`
	TotalUSD        float64  `json:"total_usd"`
}

// CostOf is a run's cost.
func CostOf(d RunDetail) Cost {
	var c Cost
	if d.Run.Budget != nil {
		c.JudgeUSD = d.Run.Budget.SpentUSD
	}
	if d.Summary != nil {
		for _, t := range []SideTotals{d.Summary.Baseline, d.Summary.Candidate} {
			if t.CostUSD != nil {
				if c.AgentUSD == nil {
					c.AgentUSD = new(float64)
				}
				*c.AgentUSD += *t.CostUSD
				c.AgentCasesKnown += t.CostKnown
			}
		}
	}
	c.TotalUSD = c.JudgeUSD
	if c.AgentUSD != nil {
		c.TotalUSD += *c.AgentUSD
	}
	return c
}

// Scenarios names a suite's runnable scenarios, sorted.
func Scenarios(suite []Entry) []string {
	var out []string
	for _, e := range suite {
		if e.Runnable() {
			out = append(out, e.ScenarioName)
		}
	}
	slices.Sort(out)
	return out
}
