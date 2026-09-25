// Package impact turns a change set into the scenarios it requires, each
// with why (spec §22 "Final impacted suite"): the union of the scenarios the
// dependency graph links to the changed components (directly or through
// dependencies), known production regressions, scenarios semantically close
// to what changed, and the suite the gate policy always runs.
//
// It is pure. The control plane asks the graph service for the blast radius
// and the simulation service for the scenarios (by name, tag, source and
// similarity); this package decides what to ask them and merges the answers.
package impact

import (
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// Bounds of what is asked of the graph and simulation services.
const (
	// MaxSeeds is the most changed components one blast radius enters from.
	MaxSeeds = 100
	// MaxQueries and MaxQueryText bound the similarity queries.
	MaxQueries   = 50
	MaxQueryText = 8000
	// MaxNamed is the most graph-linked scenarios asked for by name.
	MaxNamed = 500
	// MaxAffectedShown is the most affected components an impact lists.
	MaxAffectedShown = 50
)

// KnownRegressionSource is the scenario source of known production regressions.
const KnownRegressionSource = "production_regression"

// ---------------------------------------------------------------- inputs

// Version is what a change set compared, as the impact needs it.
type Version struct {
	Agent, Version string
	Tools          []domain.Tool
}

// Policy is the part of the gate policy that selects scenarios.
type Policy struct {
	AlwaysRunTags           []string `json:"always_run_tags"`
	IncludeKnownRegressions bool     `json:"include_known_regressions"`
	MaxDepth                int      `json:"max_depth"`
}

// Query is a text to find semantically close scenarios for; ID names it in
// the simulation service's answer, Item is the change it describes.
type Query struct {
	ID   string   `json:"id"`
	Text string   `json:"text"`
	Item *ItemRef `json:"-"`
}

// ItemRef names a change set item.
type ItemRef struct {
	Kind    string `json:"kind"`
	Subject string `json:"subject"`
	Change  string `json:"change"`
}

// Seeds returns the changed components the blast radius enters from. A
// change set naming more than MaxSeeds is entered from the candidate agent
// version instead (every component the version reaches), and a note says so.
func Seeds(seeds []changes.Seed, candidate Version) ([]changes.Seed, string) {
	if len(seeds) <= MaxSeeds {
		return seeds, ""
	}
	return []changes.Seed{{
			Component: changes.Ref{Kind: "AGENT_VERSION", Key: candidate.Agent + "@" + candidate.Version},
			Change:    "modified",
			Summary:   fmt.Sprintf("%d changed components", len(seeds)),
		}}, fmt.Sprintf("The change set names %d changed components, more than one blast radius enters from (%d); "+
			"the graph was entered from the candidate version, which reaches all of them.", len(seeds), MaxSeeds)
}

// Queries returns the similarity queries of a change set's items: the
// changed lines of the prompt, a changed tool's description, a retrieval
// source, a dependency, a declared policy, evaluator or dataset, the changed
// files. Model, parameter and limit changes are not about any topic; the
// graph links them to the agent's scenarios. A note says when items were
// left out to stay within MaxQueries.
func Queries(items []changes.Item, base, candidate Version) ([]Query, string) {
	var out []Query
	skipped := 0
	for _, it := range items {
		text := queryText(it, base, candidate)
		if strings.TrimSpace(text) == "" {
			continue
		}
		if len(out) == MaxQueries {
			skipped++
			continue
		}
		ref := &ItemRef{Kind: it.Kind, Subject: it.Subject, Change: it.Change}
		out = append(out, Query{ID: fmt.Sprintf("item-%d", len(out)+1), Text: bound(text, MaxQueryText), Item: ref})
	}
	note := ""
	if skipped > 0 {
		note = fmt.Sprintf("%d changed items were not compared with scenario descriptions (at most %d are).", skipped, MaxQueries)
	}
	return out, note
}

func bound(s string, max int) string {
	if len(s) <= max {
		return s
	}
	s = s[:max]
	for !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

// detail is an item's detail as JSON decodes it, whether the item was
// computed here or read back from storage.
func detail(it changes.Item) map[string]any {
	var out map[string]any
	raw, err := json.Marshal(it.Detail)
	if err != nil || json.Unmarshal(raw, &out) != nil || out == nil {
		return map[string]any{}
	}
	return out
}

func queryText(it changes.Item, base, candidate Version) string {
	d := detail(it)
	switch it.Kind {
	case "prompt":
		// The changed lines (as stored: secrets masked); without them
		// (only hashes are known) there is nothing to compare.
		lines, _ := d["diff"].([]any)
		var changed []string
		for _, l := range lines {
			if m, ok := l.(map[string]any); ok && m["op"] != " " {
				if t, _ := m["text"].(string); strings.TrimSpace(t) != "" {
					changed = append(changed, t)
				}
			}
		}
		if len(changed) == 0 {
			return ""
		}
		return strings.Join(append(changed, strs(d["mentions"])...), "\n")
	case "tool":
		t, ok := findTool(candidate.Tools, it.Subject)
		if !ok {
			t, ok = findTool(base.Tools, it.Subject)
		}
		text := "tool " + it.Subject + " " + it.Change + ": " + it.Summary
		if ok && t.Description != "" {
			text += "\n" + t.Description
		}
		return text
	case "retrieval_source":
		return "retrieval source " + it.Subject + " " + it.Change
	case "dependency", "policy", "evaluator", "dataset":
		return it.Kind + " " + it.Subject + " " + it.Change + ": " + it.Summary
	case "code":
		// Only file names are known; their words say what the code is about.
		return strings.Join(strs(d["changed_files"]), "\n")
	}
	return ""
}

func strs(v any) []string {
	var out []string
	l, _ := v.([]any)
	for _, x := range l {
		if s, ok := x.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func findTool(tools []domain.Tool, name string) (domain.Tool, bool) {
	for _, t := range tools {
		if t.Name == name {
			return t, true
		}
	}
	return domain.Tool{}, false
}

// ---------------------------------------------------------------- upstream answers

// GraphReason is why the graph links a scenario (graph-service's Reason).
type GraphReason struct {
	Via      changes.Ref       `json:"via"`
	ViaLabel string            `json:"via_label"`
	Direct   bool              `json:"direct"`
	Score    float64           `json:"score"`
	Path     []json.RawMessage `json:"path"`
}

// GraphLinked is a scenario, policy or evaluator the graph links to the change.
type GraphLinked struct {
	Component changes.Ref   `json:"component"`
	Label     string        `json:"label"`
	Severity  string        `json:"severity,omitempty"`
	Weight    float64       `json:"weight"`
	Reasons   []GraphReason `json:"reasons"`
}

// Affected is a component the change reaches (graph-service's Affected).
type Affected struct {
	Component  changes.Ref       `json:"component"`
	Label      string            `json:"label"`
	Attributes map[string]any    `json:"attributes"`
	Seed       bool              `json:"seed"`
	Direct     bool              `json:"direct"`
	Depth      int               `json:"depth"`
	Score      float64           `json:"score"`
	Severity   string            `json:"severity"`
	Certain    bool              `json:"certain"`
	Factors    []string          `json:"factors"`
	Path       []json.RawMessage `json:"path"`
}

// Graph is the graph service's blast radius.
type Graph struct {
	Seeds               []changes.Seed `json:"seeds"`
	Unresolved          []changes.Seed `json:"unresolved"`
	Affected            []Affected     `json:"affected"`
	Scenarios           []GraphLinked  `json:"scenarios"`
	Policies            []GraphLinked  `json:"policies"`
	Evaluators          []GraphLinked  `json:"evaluators"`
	IrreversibleActions []Affected     `json:"irreversible_actions"`
	MaxDepth            int            `json:"max_depth"`
	Truncated           bool           `json:"truncated"`
}

// Similarity is a query a scenario is close to.
type Similarity struct {
	Query      string  `json:"query"`
	Similarity float64 `json:"similarity"`
}

// MatchedScenario is a scenario the simulation service selected.
type MatchedScenario struct {
	ID            string   `json:"id"`
	Name          string   `json:"name"`
	Agent         *string  `json:"agent"`
	Twin          *string  `json:"twin"`
	Severity      string   `json:"severity"`
	Tags          []string `json:"tags"`
	Source        string   `json:"source"`
	LatestVersion int      `json:"latest_version"`
	// LatestVersionID is the version a selection pins (a release runs it).
	LatestVersionID string `json:"latest_version_id"`
	Description     string `json:"description"`
	Matched         struct {
		Name    bool         `json:"name"`
		Tags    []string     `json:"tags"`
		Source  bool         `json:"source"`
		Similar []Similarity `json:"similar"`
	} `json:"matched"`
}

// Match is the simulation service's answer.
type Match struct {
	EmbeddingModel    string            `json:"embedding_model"`
	MinSimilarity     float64           `json:"min_similarity"`
	Scenarios         []MatchedScenario `json:"scenarios"`
	UnknownNames      []string          `json:"unknown_names"`
	UnembeddedQueries []string          `json:"unembedded_queries"`
	Truncated         bool              `json:"truncated"`
}

// MatchRequest is what the simulation service is asked.
type MatchRequest struct {
	ProjectID string   `json:"project_id"`
	Agent     string   `json:"agent"`
	Names     []string `json:"names"`
	Tags      []string `json:"tags"`
	Sources   []string `json:"sources"`
	Queries   []Query  `json:"queries"`
}

// NewMatchRequest asks for the graph-linked scenarios by name, the
// always-run tags, the known regressions (when the policy includes them)
// and the queries.
func NewMatchRequest(projectID, agent string, g *Graph, pol Policy, queries []Query) (MatchRequest, string) {
	req := MatchRequest{ProjectID: projectID, Agent: agent, Names: []string{}, Tags: pol.AlwaysRunTags,
		Sources: []string{}, Queries: queries}
	if req.Tags == nil {
		req.Tags = []string{}
	}
	if pol.IncludeKnownRegressions {
		req.Sources = []string{KnownRegressionSource}
	}
	if req.Queries == nil {
		req.Queries = []Query{}
	}
	note := ""
	if g != nil {
		for _, s := range g.Scenarios {
			if len(req.Names) == MaxNamed {
				note = fmt.Sprintf("The graph links %d scenarios; the %d most weighted were selected by name.", len(g.Scenarios), MaxNamed)
				break
			}
			req.Names = append(req.Names, s.Component.Key)
		}
	}
	return req, note
}

// ---------------------------------------------------------------- the impact

// ScenarioGraphReason is a graph link of a selected scenario: the scenario
// tests Via, which the change to From reaches in Hops edges (the last edge
// is the test's own). Direct is the graph's: the link does not go only
// through the agent, nor through a tool a prompt change does not name.
type ScenarioGraphReason struct {
	From      changes.Ref       `json:"from"`
	FromLabel string            `json:"from_label"`
	Via       changes.Ref       `json:"via"`
	ViaLabel  string            `json:"via_label"`
	Direct    bool              `json:"direct"`
	Hops      int               `json:"hops"`
	Score     float64           `json:"score"`
	Path      []json.RawMessage `json:"path"`
}

// ScenarioSimilarity is a change a selected scenario is semantically close to.
type ScenarioSimilarity struct {
	Item       ItemRef `json:"item"`
	Similarity float64 `json:"similarity"`
}

// Reasons are every reason a scenario was selected for.
type Reasons struct {
	Graph           []ScenarioGraphReason `json:"graph"`
	Similar         []ScenarioSimilarity  `json:"similar"`
	AlwaysRunTags   []string              `json:"always_run_tags"`
	KnownRegression bool                  `json:"known_regression"`
}

// Scenario is a selected scenario.
type Scenario struct {
	ID            string   `json:"id,omitempty"`
	Name          string   `json:"name"`
	Agent         *string  `json:"agent"`
	Twin          *string  `json:"twin"`
	Severity      string   `json:"severity"`
	Tags          []string `json:"tags"`
	Source        string   `json:"source"`
	LatestVersion int      `json:"latest_version,omitempty"`
	// LatestVersionID is the scenario version the impact selected: what a
	// release evaluating this impact runs (empty when not in the library).
	LatestVersionID string `json:"latest_version_id,omitempty"`
	Description     string `json:"description"`
	// InLibrary is false for a scenario the graph links but the scenario
	// library cannot confirm (the simulation service did not answer).
	InLibrary bool     `json:"in_library"`
	Reasons   Reasons  `json:"reasons"`
	Why       []string `json:"why"`
}

// Privilege is a tool the candidate may use with more power than before.
type Privilege struct {
	Tool   string           `json:"tool"`
	Change string           `json:"change"`
	Risk   domain.RiskLevel `json:"risk"`
	From   domain.RiskLevel `json:"from,omitempty"`
}

// Counts summarize the selection.
type Counts struct {
	Scenarios       int `json:"scenarios"`
	Graph           int `json:"graph"`
	Similar         int `json:"similar"`
	AlwaysRun       int `json:"always_run"`
	KnownRegression int `json:"known_regression"`
}

// GraphSummary is what the blast radius says besides scenarios.
type GraphSummary struct {
	Seeds         []changes.Seed `json:"seeds"`
	Unresolved    []changes.Seed `json:"unresolved"`
	Affected      []Affected     `json:"affected"`
	AffectedCount int            `json:"affected_count"`
	Policies      []GraphLinked  `json:"policies"`
	Evaluators    []GraphLinked  `json:"evaluators"`
	MaxDepth      int            `json:"max_depth"`
	Truncated     bool           `json:"truncated"`
}

// Impact is what a change set requires.
type Impact struct {
	Scenarios           []Scenario    `json:"scenarios"`
	Counts              Counts        `json:"counts"`
	UnlinkedScenarios   []string      `json:"unlinked_scenarios"`
	Graph               *GraphSummary `json:"graph"`
	IrreversibleActions []Affected    `json:"irreversible_actions"`
	NewPrivileges       []Privilege   `json:"new_privileges"`
	EmbeddingModel      string        `json:"embedding_model"`
	MinSimilarity       float64       `json:"min_similarity"`
	Truncated           bool          `json:"truncated"`
	Notes               []string      `json:"notes"`
}

var severityRank = map[string]int{"critical": 0, "high": 1, "medium": 2, "low": 3}

// Merge builds the impact from the graph's and the simulation service's
// answers (either may be nil: that service did not answer). Every scenario
// carries every reason it was selected for and a sentence per reason.
func Merge(items []changes.Item, g *Graph, m *Match, queries []Query) Impact {
	out := Impact{Scenarios: []Scenario{}, UnlinkedScenarios: []string{}, IrreversibleActions: []Affected{},
		NewPrivileges: NewPrivileges(items), Notes: []string{}}
	links := map[string]GraphLinked{}
	if g != nil {
		for _, s := range g.Scenarios {
			links[s.Component.Key] = s
		}
		out.Graph = summarizeGraph(g)
		if g.IrreversibleActions != nil {
			out.IrreversibleActions = g.IrreversibleActions
		}
	}
	byQuery := map[string]*ItemRef{}
	for _, q := range queries {
		byQuery[q.ID] = q.Item
	}
	if m != nil {
		out.EmbeddingModel, out.MinSimilarity, out.Truncated = m.EmbeddingModel, m.MinSimilarity, m.Truncated
		if m.UnknownNames != nil {
			out.UnlinkedScenarios = m.UnknownNames
		}
		for _, ms := range m.Scenarios {
			sc := Scenario{ID: ms.ID, Name: ms.Name, Agent: ms.Agent, Twin: ms.Twin, Severity: ms.Severity,
				Tags: nonNil(ms.Tags), Source: ms.Source, LatestVersion: ms.LatestVersion,
				LatestVersionID: ms.LatestVersionID, Description: ms.Description, InLibrary: true, Reasons: emptyReasons()}
			// The graph's links are facts, whatever the library matched the scenario by.
			if l, ok := links[ms.Name]; ok {
				sc.Reasons.Graph = graphReasons(l)
			}
			for _, s := range ms.Matched.Similar {
				if it := byQuery[s.Query]; it != nil {
					sc.Reasons.Similar = append(sc.Reasons.Similar, ScenarioSimilarity{Item: *it, Similarity: s.Similarity})
				}
			}
			sc.Reasons.AlwaysRunTags = nonNil(ms.Matched.Tags)
			sc.Reasons.KnownRegression = ms.Matched.Source && ms.Source == KnownRegressionSource
			sc.Why = why(sc.Reasons)
			if len(sc.Why) == 0 {
				// Selected for nothing this package asked for: not part of the impact.
				continue
			}
			out.Scenarios = append(out.Scenarios, sc)
		}
	} else if g != nil {
		// Without the scenario library, the graph's links are all that is known.
		for _, l := range g.Scenarios {
			sc := Scenario{Name: l.Component.Key, Severity: nonEmpty(l.Severity, "medium"), Tags: []string{},
				Reasons: emptyReasons()}
			sc.Reasons.Graph = graphReasons(l)
			sc.Why = why(sc.Reasons)
			out.Scenarios = append(out.Scenarios, sc)
		}
	}
	sort.SliceStable(out.Scenarios, func(i, j int) bool {
		a, b := out.Scenarios[i], out.Scenarios[j]
		if ra, rb := rank(a.Severity), rank(b.Severity); ra != rb {
			return ra < rb
		}
		return a.Name < b.Name
	})
	for _, s := range out.Scenarios {
		out.Counts.Scenarios++
		if len(s.Reasons.Graph) > 0 {
			out.Counts.Graph++
		}
		if len(s.Reasons.Similar) > 0 {
			out.Counts.Similar++
		}
		if len(s.Reasons.AlwaysRunTags) > 0 {
			out.Counts.AlwaysRun++
		}
		if s.Reasons.KnownRegression {
			out.Counts.KnownRegression++
		}
	}
	return out
}

func rank(severity string) int {
	if r, ok := severityRank[severity]; ok {
		return r
	}
	return len(severityRank)
}

func emptyReasons() Reasons {
	return Reasons{Graph: []ScenarioGraphReason{}, Similar: []ScenarioSimilarity{}, AlwaysRunTags: []string{}}
}

func graphReasons(l GraphLinked) []ScenarioGraphReason {
	out := make([]ScenarioGraphReason, 0, len(l.Reasons))
	for _, r := range l.Reasons {
		gr := ScenarioGraphReason{From: r.Via, FromLabel: r.ViaLabel, Via: r.Via, ViaLabel: r.ViaLabel, Direct: r.Direct,
			Hops: max(len(r.Path)-1, 0), Score: r.Score, Path: r.Path}
		// The path starts at the changed component.
		if len(r.Path) > 0 {
			var first struct {
				Component changes.Ref `json:"component"`
				Label     string      `json:"label"`
			}
			if json.Unmarshal(r.Path[0], &first) == nil && first.Component.Kind != "" {
				gr.From, gr.FromLabel = first.Component, first.Label
			}
		}
		out = append(out, gr)
	}
	return out
}

// why says each reason in a sentence. Weaker graph links (through the
// agent, or a tool the change does not name) are said only when there is
// no stronger one; reasons lists them all.
func why(r Reasons) []string {
	var out []string
	strong := slices.ContainsFunc(r.Graph, func(g ScenarioGraphReason) bool { return g.Direct })
	for _, g := range r.Graph {
		if strong && !g.Direct {
			continue
		}
		via, from := describe(g.Via, g.ViaLabel), describe(g.From, g.FromLabel)
		var s string
		switch {
		case g.Via == g.From:
			s = fmt.Sprintf("tests %s, which changed", via)
		case g.Hops <= 2:
			s = fmt.Sprintf("tests %s, which the change to %s reaches directly", via, from)
		default:
			s = fmt.Sprintf("tests %s, which the change to %s reaches in %d steps", via, from, g.Hops-1)
		}
		if !g.Direct {
			s += " (a weaker link: through the agent, or a tool the change does not name)"
		}
		out = append(out, s)
	}
	for _, s := range r.Similar {
		out = append(out, fmt.Sprintf("its description is close to the change of %s (similarity %.2f)", describeItem(s.Item), s.Similarity))
	}
	if len(r.AlwaysRunTags) > 0 {
		out = append(out, "always runs (tagged "+strings.Join(r.AlwaysRunTags, ", ")+")")
	}
	if r.KnownRegression {
		out = append(out, "a known production regression")
	}
	return out
}

func describe(ref changes.Ref, label string) string {
	kind := strings.ToLower(strings.ReplaceAll(ref.Kind, "_", " "))
	name := ref.Key
	if label != "" && ref.Kind != "PROMPT" {
		name = label
	}
	if ref.Kind == "PROMPT" {
		return "the prompt"
	}
	return kind + " " + name
}

func describeItem(it ItemRef) string {
	switch it.Kind {
	case "prompt":
		return "the prompt"
	case "code":
		return "the code"
	}
	return strings.ReplaceAll(it.Kind, "_", " ") + " " + it.Subject
}

// NewPrivileges lists the tools the candidate uses with more power than
// the base: added tools that do more than read, and tools whose risk rose.
func NewPrivileges(items []changes.Item) []Privilege {
	out := []Privilege{}
	for _, it := range items {
		if it.Kind != "tool" {
			continue
		}
		d := detail(it)
		switch it.Change {
		case "added":
			if d["new_privilege"] == true {
				r, _ := d["risk"].(string)
				out = append(out, Privilege{Tool: it.Subject, Change: "added", Risk: domain.RiskLevel(r)})
			}
		case "modified":
			risk, _ := d["risk"].(map[string]any)
			if risk != nil && risk["escalated"] == true {
				to, _ := risk["to"].(string)
				from, _ := risk["from"].(string)
				out = append(out, Privilege{Tool: it.Subject, Change: "escalated", Risk: domain.RiskLevel(to),
					From: domain.RiskLevel(from)})
			}
		}
	}
	return out
}

func summarizeGraph(g *Graph) *GraphSummary {
	s := &GraphSummary{Seeds: nonNilSeeds(g.Seeds), Unresolved: nonNilSeeds(g.Unresolved), Affected: []Affected{},
		AffectedCount: len(g.Affected), Policies: nonNilLinked(g.Policies), Evaluators: nonNilLinked(g.Evaluators),
		MaxDepth: g.MaxDepth, Truncated: g.Truncated}
	affected := append([]Affected(nil), g.Affected...)
	sort.SliceStable(affected, func(i, j int) bool { return affected[i].Score > affected[j].Score })
	if len(affected) > MaxAffectedShown {
		affected = affected[:MaxAffectedShown]
	}
	if affected != nil {
		s.Affected = affected
	}
	return s
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func nonNilSeeds(s []changes.Seed) []changes.Seed {
	if s == nil {
		return []changes.Seed{}
	}
	return s
}

func nonNilLinked(s []GraphLinked) []GraphLinked {
	if s == nil {
		return []GraphLinked{}
	}
	return s
}

func nonEmpty(s, def string) string {
	if s == "" {
		return def
	}
	return s
}
