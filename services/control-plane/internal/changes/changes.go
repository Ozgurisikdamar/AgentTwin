// Package changes computes what changed between two registered versions of
// an agent (spec §21): the prompt, the model and its parameters, limits,
// tools and their schemas, risk and permissions, retrieval sources,
// dependencies and code, plus the policy, evaluator and dataset changes an
// author declares. It is pure: the caller loads the two versions.
//
// Every change also becomes a seed of the blast-radius traversal (spec §22),
// scoped to the candidate version.
package changes

import (
	"fmt"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/redact"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// Version is one side of a change set.
type Version struct {
	Manifest domain.Manifest
	// PromptText is the prompt when the project stores prompt text; nil when
	// only its hash is known.
	PromptText *string
	CommitSHA  *string
}

// Git is what a CI system says about the code between the two versions.
// File names are not understood: they are recorded as such.
type Git struct {
	BaseCommit      string   `json:"base_commit,omitempty"`
	CandidateCommit string   `json:"candidate_commit,omitempty"`
	ChangedFiles    []string `json:"changed_files,omitempty"`
}

// Declared is a change the author states that no agent version carries: a
// policy, an evaluator or a dataset.
type Declared struct {
	Kind    string `json:"kind"` // policy | evaluator | dataset
	Name    string `json:"name"`
	Change  string `json:"change"` // added | removed | modified
	Summary string `json:"summary,omitempty"`
}

// Confidence says how a change is known.
const (
	// ConfidenceExact: both versions are registered and compared field by field.
	ConfidenceExact = "exact"
	// ConfidenceHashOnly: the content changed but its text is not stored.
	ConfidenceHashOnly = "hash_only"
	// ConfidenceFilenames: only the names of changed files are known.
	ConfidenceFilenames = "filenames_only"
	// ConfidenceDeclared: stated by the author, not detected.
	ConfidenceDeclared = "declared"
)

// Item is one change.
type Item struct {
	Kind       string         `json:"kind"`
	Subject    string         `json:"subject"`
	Change     string         `json:"change"`
	Summary    string         `json:"summary"`
	Confidence string         `json:"confidence"`
	Breaking   bool           `json:"breaking"`
	Detail     map[string]any `json:"detail"`
}

// Ref names a graph component (graph-service's reference).
type Ref struct {
	Kind string `json:"kind"`
	Key  string `json:"key"`
}

// Seed is a changed component for the blast-radius traversal
// (graph-service's Change).
type Seed struct {
	Component Ref      `json:"component"`
	Change    string   `json:"change"`
	Summary   string   `json:"summary,omitempty"`
	Mentions  []string `json:"mentions,omitempty"`
}

// Scope restricts the traversal to the candidate version.
type Scope struct {
	Agent   string `json:"agent"`
	Version string `json:"version"`
}

// Set is a computed change set.
type Set struct {
	Items []Item `json:"items"`
	Seeds []Seed `json:"seeds"`
	Scope Scope  `json:"scope"`
}

// Kinds in the order items are listed.
var Kinds = []string{"prompt", "model", "model_params", "limits", "tool", "retrieval_source", "dependency", "code",
	"policy", "evaluator", "dataset"}

// Bounds on what one change set records.
const (
	MaxChangedFiles = 1000
	maxFilesShown   = 200
)

// Compute compares two versions of the same agent.
func Compute(base, candidate Version, git *Git, declared []Declared) Set {
	c := &computer{base: base.Manifest, cand: candidate.Manifest}
	c.prompt(base.PromptText, candidate.PromptText)
	c.model()
	c.limits()
	c.tools()
	c.retrieval()
	c.dependencies()
	c.code(base.CommitSHA, candidate.CommitSHA, git)
	for _, d := range declared {
		c.item(Item{Kind: d.Kind, Subject: d.Name, Change: d.Change, Confidence: ConfidenceDeclared,
			Summary: nonEmpty(d.Summary, fmt.Sprintf("%s %s %s", d.Kind, d.Name, d.Change)), Detail: map[string]any{}},
			Seed{Component: Ref{graphKind[d.Kind], d.Name}, Change: d.Change, Summary: d.Summary})
	}
	order := map[string]int{}
	for i, k := range Kinds {
		order[k] = i
	}
	sort.SliceStable(c.set.Items, func(i, j int) bool {
		a, b := c.set.Items[i], c.set.Items[j]
		if a.Kind != b.Kind {
			return order[a.Kind] < order[b.Kind]
		}
		return a.Subject < b.Subject
	})
	c.set.Scope = Scope{Agent: c.cand.Name, Version: c.cand.Version}
	if c.set.Items == nil {
		c.set.Items = []Item{}
	}
	if c.set.Seeds == nil {
		c.set.Seeds = []Seed{}
	}
	return c.set
}

var graphKind = map[string]string{"policy": "POLICY", "evaluator": "EVALUATOR", "dataset": "DATASET"}

type computer struct {
	base, cand domain.Manifest
	set        Set
}

func (c *computer) item(it Item, seeds ...Seed) {
	if it.Detail == nil {
		it.Detail = map[string]any{}
	}
	c.set.Items = append(c.set.Items, it)
	for _, s := range seeds {
		if !slices.ContainsFunc(c.set.Seeds, func(x Seed) bool { return x.Component == s.Component && x.Change == s.Change }) {
			c.set.Seeds = append(c.set.Seeds, s)
		}
	}
}

func (c *computer) candidateVersion() Ref {
	return Ref{"AGENT_VERSION", c.cand.Name + "@" + c.cand.Version}
}

// ---------------------------------------------------------------- prompt

var redactor = redact.All()

func (c *computer) prompt(baseText, candText *string) {
	a, b := c.base.PromptSHA256, c.cand.PromptSHA256
	if a == b && (baseText == nil || candText == nil || *baseText == *candText) {
		return
	}
	it := Item{Kind: "prompt", Subject: nonEmpty(b, a), Change: "modified", Confidence: ConfidenceHashOnly,
		Detail: map[string]any{"base_sha256": nilIfEmpty(a), "candidate_sha256": nilIfEmpty(b)}}
	switch {
	case a == "":
		it.Change = "added"
	case b == "":
		it.Change = "removed"
	}
	var mentions []string
	if baseText != nil && candText != nil {
		if lines, truncated, ok := DiffText(*baseText, *candText); ok {
			it.Confidence = ConfidenceExact
			changed := make([]string, 0, len(lines))
			for i := range lines {
				if lines[i].Op != " " {
					changed = append(changed, lines[i].Text)
				}
				// The diff is shown to people: secrets and personal data in
				// a prompt are masked like any captured content.
				lines[i].Text, _ = redactor.String(lines[i].Text)
			}
			it.Detail["diff"] = lines
			it.Detail["diff_truncated"] = truncated
			mentions = c.mentioned(changed)
			it.Detail["mentions"] = mentions
		} else {
			it.Detail["diff_unavailable"] = "too long to diff"
		}
	} else {
		it.Detail["diff_unavailable"] = "the project does not store prompt text"
	}
	it.Summary = "prompt " + it.Change
	if len(mentions) > 0 {
		it.Summary += "; changed lines mention " + strings.Join(mentions, ", ")
	}
	var seed Seed
	switch {
	case b != "":
		seed = Seed{Component: Ref{"PROMPT", b}, Change: it.Change, Summary: it.Summary, Mentions: mentions}
	default:
		// The candidate has no prompt: the version itself changed.
		seed = Seed{Component: c.candidateVersion(), Change: "modified", Summary: it.Summary}
	}
	c.item(it, seed)
}

// mentioned returns the tools of either version named in the changed lines.
func (c *computer) mentioned(changed []string) []string {
	text := strings.ToLower(strings.Join(changed, "\n"))
	var out []string
	for _, name := range c.toolNames() {
		re := regexp.MustCompile(`(^|[^a-z0-9_-])` + regexp.QuoteMeta(name) + `($|[^a-z0-9_-])`)
		if re.MatchString(text) {
			out = append(out, name)
		}
	}
	return out
}

func (c *computer) toolNames() []string {
	var names []string
	for _, t := range append(slices.Clone(c.base.Tools), c.cand.Tools...) {
		if !slices.Contains(names, t.Name) {
			names = append(names, t.Name)
		}
	}
	sort.Strings(names)
	return names
}

// ---------------------------------------------------------------- model

func (c *computer) model() {
	a, b := c.base.Model, c.cand.Model
	ka, kb := a.Provider+"/"+a.Name, b.Provider+"/"+b.Name
	if ka != kb {
		c.item(Item{Kind: "model", Subject: kb, Change: "modified", Confidence: ConfidenceExact,
			Summary: fmt.Sprintf("model %s → %s", ka, kb), Detail: map[string]any{"from": ka, "to": kb}},
			Seed{Component: Ref{"MODEL", kb}, Change: "modified", Summary: fmt.Sprintf("model %s → %s", ka, kb)})
	}
	var params []string
	if !reflect.DeepEqual(a.Temperature, b.Temperature) {
		params = append(params, "temperature")
	}
	if !reflect.DeepEqual(a.MaxTokens, b.MaxTokens) {
		params = append(params, "max_tokens")
	}
	for _, k := range unionKeys(a.Params, b.Params) {
		if !reflect.DeepEqual(norm(a.Params[k]), norm(b.Params[k])) {
			params = append(params, k)
		}
	}
	if len(params) > 0 {
		summary := "model parameters changed: " + strings.Join(params, ", ")
		c.item(Item{Kind: "model_params", Subject: kb, Change: "modified", Confidence: ConfidenceExact, Summary: summary,
			Detail: map[string]any{"parameters": params,
				"from": modelParams(a), "to": modelParams(b)}},
			Seed{Component: c.candidateVersion(), Change: "modified", Summary: summary})
	}
}

func modelParams(m domain.Model) map[string]any {
	out := map[string]any{}
	for k, v := range m.Params {
		out[k] = v
	}
	if m.Temperature != nil {
		out["temperature"] = *m.Temperature
	}
	if m.MaxTokens != nil {
		out["max_tokens"] = *m.MaxTokens
	}
	return out
}

func (c *computer) limits() {
	a, b := c.base.Limits, c.cand.Limits
	var changed []string
	for name, pair := range map[string][2]any{
		"max_steps": {a.MaxSteps, b.MaxSteps}, "max_tool_calls": {a.MaxToolCalls, b.MaxToolCalls},
		"max_duration_seconds": {a.MaxDurationSeconds, b.MaxDurationSeconds}, "max_cost_usd": {a.MaxCostUSD, b.MaxCostUSD},
	} {
		if !reflect.DeepEqual(pair[0], pair[1]) {
			changed = append(changed, name)
		}
	}
	if len(changed) == 0 {
		return
	}
	sort.Strings(changed)
	summary := "limits changed: " + strings.Join(changed, ", ")
	c.item(Item{Kind: "limits", Subject: c.cand.Name, Change: "modified", Confidence: ConfidenceExact, Summary: summary,
		Detail: map[string]any{"limits": changed, "from": a, "to": b}},
		Seed{Component: c.candidateVersion(), Change: "modified", Summary: summary})
}

// ---------------------------------------------------------------- tools

func (c *computer) tools() {
	for _, name := range c.toolNames() {
		a, inA := c.base.Tool(name)
		b, inB := c.cand.Tool(name)
		tool := Ref{"TOOL", name}
		switch {
		case !inA:
			summary := fmt.Sprintf("tool added (%s)", b.Risk)
			c.item(Item{Kind: "tool", Subject: name, Change: "added", Confidence: ConfidenceExact, Summary: summary,
				Detail: map[string]any{"risk": b.Risk, "new_privilege": b.Risk != domain.RiskRead}},
				Seed{Component: tool, Change: "added", Summary: summary})
		case !inB:
			c.item(Item{Kind: "tool", Subject: name, Change: "removed", Confidence: ConfidenceExact, Summary: "tool removed",
				Detail: map[string]any{"risk": a.Risk}},
				Seed{Component: tool, Change: "removed", Summary: "tool removed"})
		case a.DefinitionSHA != b.DefinitionSHA:
			it := toolChange(a, b)
			c.item(it, Seed{Component: tool, Change: "modified", Summary: it.Summary})
		}
	}
}

// toolChange describes what changed in a tool both versions use (spec §118).
func toolChange(a, b domain.Tool) Item {
	it := Item{Kind: "tool", Subject: b.Name, Change: "modified", Confidence: ConfidenceExact, Detail: map[string]any{}}
	var aspects, parts []string
	if a.Risk != b.Risk {
		escalated := domain.RiskRank(b.Risk) > domain.RiskRank(a.Risk)
		aspects = append(aspects, "risk")
		parts = append(parts, fmt.Sprintf("risk %s → %s", a.Risk, b.Risk))
		it.Detail["risk"] = map[string]any{"from": a.Risk, "to": b.Risk, "escalated": escalated}
		it.Detail["new_privilege"] = escalated
	}
	if !reflect.DeepEqual(a.Dimensions, b.Dimensions) {
		aspects = append(aspects, "permissions")
		parts = append(parts, "permissions changed")
		it.Detail["permissions"] = map[string]any{"from": a.Dimensions, "to": b.Dimensions}
	}
	if a.ApprovalWhen != b.ApprovalWhen {
		aspects = append(aspects, "approval")
		parts = append(parts, "approval rule changed")
		it.Detail["approval"] = map[string]any{"from": nilIfEmpty(a.ApprovalWhen), "to": nilIfEmpty(b.ApprovalWhen)}
	}
	if !reflect.DeepEqual(norm(a.Compensating), norm(b.Compensating)) {
		aspects = append(aspects, "compensating_action")
		parts = append(parts, "compensating action changed")
	}
	if a.Description != b.Description {
		// The model reads the description: a change can change when it
		// calls the tool.
		aspects = append(aspects, "description")
		parts = append(parts, "description changed")
	}
	if sc := DiffSchemas(a.InputSchema, b.InputSchema); len(sc) > 0 {
		aspects = append(aspects, "schema")
		breaking := 0
		for _, s := range sc {
			if s.Breaking {
				breaking++
			}
		}
		it.Breaking = breaking > 0
		it.Detail["schema_changes"] = sc
		parts = append(parts, fmt.Sprintf("%d schema changes (%d breaking)", len(sc), breaking))
	}
	if a.Version != b.Version {
		aspects = append(aspects, "version")
		it.Detail["version"] = map[string]any{"from": nilIfEmpty(a.Version), "to": nilIfEmpty(b.Version)}
	}
	if len(parts) == 0 {
		parts = append(parts, "definition changed")
	}
	it.Detail["aspects"] = aspects
	it.Summary = strings.Join(parts, "; ")
	return it
}

// ---------------------------------------------------------------- retrieval, dependencies

func (c *computer) retrieval() {
	a, b := c.base.RetrievalSources, c.cand.RetrievalSources
	for _, s := range b {
		if !slices.Contains(a, s) {
			c.item(Item{Kind: "retrieval_source", Subject: s, Change: "added", Confidence: ConfidenceExact, Summary: "retrieval source added"},
				Seed{Component: Ref{"RETRIEVAL_SOURCE", s}, Change: "added", Summary: "retrieval source added"})
		}
	}
	for _, s := range a {
		if !slices.Contains(b, s) {
			c.item(Item{Kind: "retrieval_source", Subject: s, Change: "removed", Confidence: ConfidenceExact, Summary: "retrieval source removed"},
				Seed{Component: Ref{"RETRIEVAL_SOURCE", s}, Change: "removed", Summary: "retrieval source removed"})
		}
	}
}

func depKey(d domain.Dependency) string {
	return d.Tool + " → " + strings.ToUpper(d.Kind) + ":" + d.Name
}

func (c *computer) dependencies() {
	index := func(ds []domain.Dependency) map[string]domain.Dependency {
		m := map[string]domain.Dependency{}
		for _, d := range ds {
			m[depKey(d)] = d
		}
		return m
	}
	a, b := index(c.base.Dependencies), index(c.cand.Dependencies)
	var keys []string
	for k := range a {
		keys = append(keys, k)
	}
	for k := range b {
		if _, ok := a[k]; !ok {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		da, inA := a[k]
		db, inB := b[k]
		var change, summary string
		detail := map[string]any{}
		switch {
		case !inA:
			change, summary = "added", fmt.Sprintf("%s now %s %s", db.Tool, strings.ToLower(db.Relation), db.Name)
			detail["to"] = db
		case !inB:
			change, summary = "removed", fmt.Sprintf("%s no longer %s %s", da.Tool, strings.ToLower(da.Relation), da.Name)
			detail["from"] = da
		case da.Relation != db.Relation || da.Critical != db.Critical:
			change, summary = "modified", fmt.Sprintf("%s → %s: %s/%s → %s/%s", da.Tool, da.Name, da.Relation, nonEmpty(da.Critical, "-"), db.Relation, nonEmpty(db.Critical, "-"))
			detail["from"], detail["to"] = da, db
		default:
			continue
		}
		tool := nonEmpty(db.Tool, da.Tool)
		c.item(Item{Kind: "dependency", Subject: k, Change: change, Confidence: ConfidenceExact, Summary: summary, Detail: detail},
			// The tool reaches the system: seeding the tool reaches both.
			Seed{Component: Ref{"TOOL", tool}, Change: "modified", Summary: summary})
	}
}

// ---------------------------------------------------------------- code

func (c *computer) code(baseSHA, candSHA *string, git *Git) {
	from, to := deref(baseSHA), deref(candSHA)
	var files []string
	if git != nil {
		from, to = nonEmpty(git.BaseCommit, from), nonEmpty(git.CandidateCommit, to)
		files = git.ChangedFiles
	}
	if (from == to || from == "" || to == "") && len(files) == 0 {
		return
	}
	detail := map[string]any{"base_commit": nilIfEmpty(from), "candidate_commit": nilIfEmpty(to), "changed_file_count": len(files)}
	shown := files
	if len(shown) > maxFilesShown {
		shown = shown[:maxFilesShown]
	}
	detail["changed_files"] = shown
	detail["changed_files_truncated"] = len(files) > len(shown)
	summary := "code changed"
	if len(files) > 0 {
		summary = fmt.Sprintf("code changed: %d files (names only)", len(files))
	}
	// Only file names are known, so nothing narrower than the whole agent
	// can be said to be affected (spec §21).
	c.item(Item{Kind: "code", Subject: nonEmpty(to, from), Change: "modified", Confidence: ConfidenceFilenames, Summary: summary, Detail: detail},
		Seed{Component: c.candidateVersion(), Change: "modified", Summary: summary})
}

// ---------------------------------------------------------------- helpers

func nonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func unionKeys(a, b map[string]any) []string {
	var out []string
	for k := range a {
		out = append(out, k)
	}
	for k := range b {
		if _, ok := a[k]; !ok {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}
