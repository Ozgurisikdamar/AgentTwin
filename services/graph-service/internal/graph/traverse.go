package graph

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strings"
)

// Reader is the traversal's view of a project's graph. Implementations
// answer for one organization and project.
type Reader interface {
	// Lookup resolves references to nodes; unknown references are absent.
	Lookup(ctx context.Context, refs []Ref) (map[Ref]Node, error)
	// Nodes loads nodes by id; unknown ids are absent.
	Nodes(ctx context.Context, ids []string) (map[string]Node, error)
	// Edges returns the edges of the given types leaving (out) or entering
	// (!out) any of ids.
	Edges(ctx context.Context, ids []string, out bool, types []EdgeType) ([]Edge, error)
}

// Change is one changed component, the seed of a traversal.
type Change struct {
	Ref Ref `json:"component"`
	// Change is "added", "removed" or "modified".
	Change string `json:"change"`
	// Summary says what changed, in words ("instructions changed").
	Summary string `json:"summary,omitempty"`
	// Mentions names the tools a changed text mentions (the changed lines of
	// a prompt). The agent's other tools are still reached, as indirect.
	Mentions []string `json:"mentions,omitempty"`
}

// Changes allowed on a seed.
var ChangeKinds = []string{"added", "removed", "modified"}

// Scope restricts which agent versions a traversal may enter.
type Scope struct {
	Agent   string `json:"agent"`
	Version string `json:"version"`
}

// Options bound a traversal.
type Options struct {
	// MaxDepth bounds the hops from a seed (spec §22: default 4).
	MaxDepth int
	// MaxNodes bounds the affected components; beyond it the result is
	// marked truncated.
	MaxNodes int
	// Scope, when set, admits only that agent version; otherwise only the
	// latest version of each agent (a node attribute "latest").
	Scope *Scope
}

// Defaults for Options.
const (
	DefaultMaxDepth = 4
	MaxMaxDepth     = 8
	DefaultMaxNodes = 500
)

// Step is one hop of a path: the edge taken and the node reached.
type Step struct {
	Node       Ref      `json:"component"`
	Label      string   `json:"label"`
	Edge       EdgeType `json:"edge"`
	Direction  string   `json:"direction"` // "down": what it acts on · "up": who depends on it
	Confidence float64  `json:"confidence"`
	Sources    []Source `json:"sources"`
}

// MarshalJSON writes the seed step (no edge taken) with a null edge,
// direction and confidence rather than empty values.
func (s Step) MarshalJSON() ([]byte, error) {
	type step struct {
		Node       Ref       `json:"component"`
		Label      string    `json:"label"`
		Edge       *EdgeType `json:"edge"`
		Direction  *string   `json:"direction"`
		Confidence *float64  `json:"confidence"`
		Sources    []Source  `json:"sources"`
	}
	out := step{Node: s.Node, Label: s.Label, Sources: s.Sources}
	if out.Sources == nil {
		out.Sources = []Source{}
	}
	if s.Edge != "" {
		out.Edge, out.Direction, out.Confidence = &s.Edge, &s.Direction, &s.Confidence
	}
	return json.Marshal(out)
}

// Affected is a component the change can influence and why.
type Affected struct {
	Node       Node           `json:"-"`
	Ref        Ref            `json:"component"`
	Label      string         `json:"label"`
	Attributes map[string]any `json:"attributes"`
	Seed       bool           `json:"seed"`
	Direct     bool           `json:"direct"`
	Depth      int            `json:"depth"`
	Score      float64        `json:"score"`
	Severity   string         `json:"severity"`
	Certain    bool           `json:"certain"`
	Factors    []string       `json:"factors"`
	// Path starts at the seed (Path[0], with no edge) and ends here.
	Path []Step `json:"path"`
}

// Reason is why a scenario, policy or evaluator was reached.
type Reason struct {
	Via    Ref     `json:"via"`
	Label  string  `json:"via_label"`
	Direct bool    `json:"direct"`
	Score  float64 `json:"score"`
	Path   []Step  `json:"path"`
}

// Linked is a scenario, policy or evaluator attached to affected components.
type Linked struct {
	Ref      Ref      `json:"component"`
	Label    string   `json:"label"`
	Severity string   `json:"severity,omitempty"`
	Weight   float64  `json:"weight"`
	Reasons  []Reason `json:"reasons"`
}

// Result is a bounded blast radius.
type Result struct {
	Seeds      []Change   `json:"seeds"`
	Unresolved []Change   `json:"unresolved"`
	Affected   []Affected `json:"affected"`
	Scenarios  []Linked   `json:"scenarios"`
	Policies   []Linked   `json:"policies"`
	Evaluators []Linked   `json:"evaluators"`
	// IrreversibleActions are the affected tools whose effects cannot be
	// undone (WRITE_IRREVERSIBLE) or that act with administrative rights.
	IrreversibleActions []Affected `json:"irreversible_actions"`
	MaxDepth            int        `json:"max_depth"`
	Truncated           bool       `json:"truncated"`
}

// Up: who depends on the component (edges read backwards). Down: what the
// component acts upon (edges read forwards).
var (
	upEdges   = []EdgeType{EdgeUses, EdgeRetrievesFrom, EdgeDependsOn, EdgeCalls, EdgeReads, EdgeWrites, EdgeCanMutate, EdgePublishes, EdgeConsumes, EdgeGuardedBy}
	downEdges = []EdgeType{EdgeUses, EdgeCalls, EdgeReads, EdgeWrites, EdgeCanMutate, EdgePublishes, EdgeDependsOn}
	linkEdges = []EdgeType{EdgeTestedBy, EdgeEvaluatedBy, EdgeGuardedBy, EdgeVersionOf}
)

// hopWeight is how strongly influence passes along an edge type.
func hopWeight(t EdgeType) float64 {
	switch t {
	case EdgeUses, EdgeWrites, EdgeCanMutate, EdgeRetrievesFrom, EdgeGuardedBy:
		return 1.0
	case EdgeCalls, EdgePublishes, EdgeDependsOn, EdgeConsumes:
		return 0.9
	case EdgeReads:
		return 0.6
	default:
		return 0.5
	}
}

const (
	depthDecay     = 0.9
	indirectFactor = 0.6
)

// configInputs change an agent's whole behaviour: an agent version reached
// from one of them acts differently through every tool it uses.
var configInputs = []Kind{KindPrompt, KindModel, KindRetrieval}

type phase int

const (
	up phase = iota
	down
)

type state struct {
	node     Node
	phase    phase
	depth    int
	direct   bool
	strength float64
	mentions []string // carried from a prompt seed to the agent version
	parent   *state
	edge     *Edge
	arrival  phase // the direction the edge was walked; the phase may continue down
	certain  bool
	seed     *Change
}

// BlastRadius computes what the changes can influence, bounded by opts.
func BlastRadius(ctx context.Context, r Reader, changes []Change, opts Options) (Result, error) {
	if opts.MaxDepth <= 0 {
		opts.MaxDepth = DefaultMaxDepth
	}
	opts.MaxDepth = min(opts.MaxDepth, MaxMaxDepth)
	if opts.MaxNodes <= 0 {
		opts.MaxNodes = DefaultMaxNodes
	}
	res := Result{MaxDepth: opts.MaxDepth, Seeds: []Change{}, Unresolved: []Change{}, Affected: []Affected{}}
	changes, err := mergeChanges(changes)
	if err != nil {
		return res, err
	}
	refs := make([]Ref, 0, len(changes))
	for _, c := range changes {
		refs = append(refs, c.Ref)
	}
	found, err := r.Lookup(ctx, refs)
	if err != nil {
		return res, err
	}

	best := map[string]*state{} // node id → best arrival (shallowest, strongest)
	seen := map[string]bool{}   // node id + phase
	key := func(id string, p phase) string { return fmt.Sprintf("%s/%d", id, p) }
	var frontier []*state
	for i := range changes {
		c := &changes[i]
		n, ok := found[c.Ref]
		if !ok {
			res.Unresolved = append(res.Unresolved, *c)
			continue
		}
		res.Seeds = append(res.Seeds, *c)
		s := &state{node: n, phase: up, direct: true, strength: 1, certain: true, seed: c}
		if n.Kind == KindPrompt {
			s.mentions = c.Mentions
		}
		seen[key(n.ID, up)] = true
		frontier = append(frontier, s)
		record(best, s)
		if startsDown(n, nil) {
			d := *s
			d.phase = down
			seen[key(n.ID, down)] = true
			frontier = append(frontier, &d)
		}
	}

	truncated := false
	for depth := 1; depth <= opts.MaxDepth && len(frontier) > 0; depth++ {
		next, err := expand(ctx, r, frontier, depth, opts.Scope)
		if err != nil {
			return res, err
		}
		slices.SortFunc(next, compareStates)
		frontier = frontier[:0]
		for _, s := range next {
			if seen[key(s.node.ID, s.phase)] {
				continue
			}
			if _, known := best[s.node.ID]; !known && len(best) >= opts.MaxNodes {
				truncated = true
				continue
			}
			seen[key(s.node.ID, s.phase)] = true
			record(best, s)
			frontier = append(frontier, s)
			// An agent version reached from a configuration input also acts
			// differently: continue down from it at the same depth.
			if s.phase == up && startsDown(s.node, s) && !seen[key(s.node.ID, down)] {
				d := *s
				d.phase = down
				seen[key(s.node.ID, down)] = true
				frontier = append(frontier, &d)
			}
		}
	}
	res.Truncated = truncated

	for _, s := range best {
		res.Affected = append(res.Affected, affected(s))
	}
	slices.SortFunc(res.Affected, compareAffected)
	for _, a := range res.Affected {
		if a.Node.Kind == KindTool && isIrreversible(a.Node) {
			res.IrreversibleActions = append(res.IrreversibleActions, a)
		}
	}
	if res.IrreversibleActions == nil {
		res.IrreversibleActions = []Affected{}
	}
	if err := link(ctx, r, &res); err != nil {
		return res, err
	}
	return res, nil
}

// mergeChanges validates the changes and merges those naming the same
// component: their mentions are combined, the first description kept.
func mergeChanges(in []Change) ([]Change, error) {
	var out []Change
	at := map[Ref]int{}
	for _, c := range in {
		if err := c.Ref.Validate(); err != nil {
			return nil, err
		}
		if !slices.Contains(ChangeKinds, c.Change) {
			return nil, fmt.Errorf("change must be added, removed or modified")
		}
		i, ok := at[c.Ref]
		if !ok {
			at[c.Ref] = len(out)
			c.Mentions = slices.Clone(c.Mentions)
			out = append(out, c)
			continue
		}
		for _, m := range c.Mentions {
			if !slices.Contains(out[i].Mentions, m) {
				out[i].Mentions = append(out[i].Mentions, m)
			}
		}
	}
	return out, nil
}

// startsDown reports whether traversal continues downwards from n: seeds do
// (other than agents and scenarios), and so does an agent version reached
// from a configuration input.
func startsDown(n Node, arrival *state) bool {
	if arrival == nil {
		return n.Kind != KindScenario && n.Kind != KindAgent
	}
	return n.Kind == KindAgentVersion && arrival.parent != nil && slices.Contains(configInputs, arrival.parent.node.Kind)
}

func record(best map[string]*state, s *state) {
	cur, ok := best[s.node.ID]
	if !ok || s.depth < cur.depth || (s.depth == cur.depth && s.strength > cur.strength) {
		best[s.node.ID] = s
	}
}

func expand(ctx context.Context, r Reader, frontier []*state, depth int, scope *Scope) ([]*state, error) {
	var ups, downs []string
	byID := map[string][]*state{}
	for _, s := range frontier {
		byID[s.node.ID] = append(byID[s.node.ID], s)
		if s.phase == up {
			ups = append(ups, s.node.ID)
		} else {
			downs = append(downs, s.node.ID)
		}
	}
	type hop struct {
		from  *state
		edge  Edge
		other string
	}
	var hops []hop
	if len(ups) > 0 {
		edges, err := r.Edges(ctx, dedupe(ups), false, upEdges)
		if err != nil {
			return nil, err
		}
		for _, e := range edges {
			for _, s := range byID[e.To] {
				if s.phase == up {
					hops = append(hops, hop{s, e, e.From})
				}
			}
		}
	}
	if len(downs) > 0 {
		edges, err := r.Edges(ctx, dedupe(downs), true, downEdges)
		if err != nil {
			return nil, err
		}
		for _, e := range edges {
			for _, s := range byID[e.From] {
				if s.phase == down {
					hops = append(hops, hop{s, e, e.To})
				}
			}
		}
	}
	ids := make([]string, 0, len(hops))
	for _, h := range hops {
		ids = append(ids, h.other)
	}
	nodes, err := r.Nodes(ctx, dedupe(ids))
	if err != nil {
		return nil, err
	}
	var out []*state
	for _, h := range hops {
		n, ok := nodes[h.other]
		if !ok || !admitted(n, scope) {
			continue
		}
		dir := down
		if h.from.phase == up {
			dir = up
		}
		// Going down, USES only reaches the tools an agent acts through.
		if dir == down && h.edge.Type == EdgeUses && n.Kind != KindTool {
			continue
		}
		direct := h.from.direct
		strength := h.from.strength * h.edge.Confidence * hopWeight(h.edge.Type)
		// A prompt change that names tools in its changed lines links those
		// tools directly; the agent's other tools are reached, as indirect.
		if dir == down && h.from.node.Kind == KindAgentVersion && n.Kind == KindTool &&
			len(h.from.mentions) > 0 && !slices.Contains(h.from.mentions, n.Key) {
			direct = false
			strength *= indirectFactor
		}
		e := h.edge
		out = append(out, &state{node: n, phase: dir, depth: depth, direct: direct, strength: strength,
			parent: h.from, edge: &e, arrival: dir, certain: h.from.certain && e.Certain(), seed: h.from.seed,
			mentions: carryMentions(h.from, n)})
	}
	return out, nil
}

func carryMentions(from *state, to Node) []string {
	if from.node.Kind == KindPrompt && to.Kind == KindAgentVersion {
		return from.mentions
	}
	return nil
}

func admitted(n Node, scope *Scope) bool {
	if n.Kind != KindAgentVersion {
		return true
	}
	if scope != nil {
		return n.Str("agent") == scope.Agent && n.Str("version") == scope.Version
	}
	latest, _ := n.Attrs["latest"].(bool)
	return latest
}

func dedupe(ids []string) []string {
	slices.Sort(ids)
	return slices.Compact(ids)
}

var kindOrder = func() map[Kind]int {
	m := map[Kind]int{}
	for i, k := range Kinds {
		m[k] = i
	}
	return m
}()

func compareStates(a, b *state) int {
	if c := cmp.Compare(b.strength, a.strength); c != 0 {
		return c
	}
	if c := cmp.Compare(kindOrder[a.node.Kind], kindOrder[b.node.Kind]); c != 0 {
		return c
	}
	if c := strings.Compare(a.node.Key, b.node.Key); c != 0 {
		return c
	}
	return strings.Compare(pathKey(a), pathKey(b))
}

func pathKey(s *state) string {
	var parts []string
	for ; s != nil; s = s.parent {
		part := s.node.ID
		if s.edge != nil {
			part += "/" + string(s.edge.Type) + "/" + s.edge.ID
		}
		parts = append(parts, part)
	}
	return strings.Join(parts, "<")
}

func compareAffected(a, b Affected) int {
	if a.Seed != b.Seed {
		if a.Seed {
			return -1
		}
		return 1
	}
	if c := cmp.Compare(b.Score, a.Score); c != 0 {
		return c
	}
	if c := cmp.Compare(a.Depth, b.Depth); c != 0 {
		return c
	}
	if c := cmp.Compare(kindOrder[a.Ref.Kind], kindOrder[b.Ref.Kind]); c != 0 {
		return c
	}
	return strings.Compare(a.Ref.Key, b.Ref.Key)
}

// criticality is how much it matters when the component is affected, from
// what is declared about it: a tool's risk tier, a dependency's criticality.
func criticality(n Node) (float64, string) {
	switch n.Kind {
	case KindTool:
		switch risk := n.Str("risk"); risk {
		case "WRITE_IRREVERSIBLE", "ADMIN":
			return 1.0, risk + " tool"
		case "EXECUTE":
			return 0.9, "EXECUTE tool"
		case "WRITE_REVERSIBLE":
			return 0.7, "WRITE_REVERSIBLE tool"
		case "READ":
			return 0.5, "READ-only tool"
		default:
			return 0.6, "tool of unknown risk"
		}
	case KindAgentVersion, KindPrompt, KindModel:
		return 1.0, ""
	}
	switch c := strings.ToUpper(n.Str("criticality")); c {
	case "CRITICAL":
		return 1.0, "criticality CRITICAL"
	case "HIGH":
		return 0.85, "criticality HIGH"
	case "MEDIUM":
		return 0.65, "criticality MEDIUM"
	case "LOW":
		return 0.45, "criticality LOW"
	}
	return 0.6, ""
}

func isIrreversible(n Node) bool {
	r := n.Str("risk")
	return r == "WRITE_IRREVERSIBLE" || r == "ADMIN"
}

// Severity buckets a score.
func Severity(score float64) string {
	switch {
	case score >= 0.75:
		return "critical"
	case score >= 0.5:
		return "high"
	case score >= 0.3:
		return "medium"
	default:
		return "low"
	}
}

func round3(v float64) float64 { return math.Round(v*1000) / 1000 }

func affected(s *state) Affected {
	crit, why := criticality(s.node)
	score := round3(s.strength * math.Pow(depthDecay, float64(s.depth)) * crit)
	attrs := s.node.Attrs
	if attrs == nil {
		attrs = map[string]any{}
	}
	a := Affected{Node: s.node, Ref: s.node.Ref(), Label: s.node.Label, Attributes: attrs, Seed: s.parent == nil, Direct: s.direct,
		Depth: s.depth, Score: score, Severity: Severity(score), Certain: s.certain, Path: pathOf(s), Factors: []string{}}
	if a.Seed {
		a.Factors = append(a.Factors, "changed: "+nonEmpty(s.seed.Summary, s.seed.Change))
	} else {
		a.Factors = append(a.Factors, fmt.Sprintf("%d hop(s) from the change", s.depth))
		if s.direct {
			a.Factors = append(a.Factors, "directly linked")
		} else {
			a.Factors = append(a.Factors, "indirect: the agent uses it but the change does not mention it")
		}
	}
	if why != "" {
		a.Factors = append(a.Factors, why)
	}
	if !s.certain {
		a.Factors = append(a.Factors, "reached through an inferred relationship")
	}
	return a
}

func nonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func pathOf(s *state) []Step {
	var rev []*state
	for c := s; c != nil; c = c.parent {
		rev = append(rev, c)
	}
	out := make([]Step, 0, len(rev))
	for i := len(rev) - 1; i >= 0; i-- {
		c := rev[i]
		st := Step{Node: c.node.Ref(), Label: c.node.Label, Sources: []Source{}}
		if c.edge != nil {
			st.Edge = c.edge.Type
			st.Confidence = c.edge.Confidence
			st.Sources = c.edge.Sources
			st.Direction = "down"
			if c.arrival == up {
				st.Direction = "up"
			}
		}
		out = append(out, st)
	}
	return out
}

// linkTarget is the only kind each collected edge may point at.
var linkTarget = map[EdgeType]Kind{EdgeTestedBy: KindScenario, EdgeEvaluatedBy: KindEvaluator, EdgeGuardedBy: KindPolicy}

var scenarioWeight = map[string]float64{"critical": 1.0, "high": 0.85, "medium": 0.7, "low": 0.5}

// link attaches the scenarios, policies and evaluators of the affected
// components. They are collected, not traversed: a test of an affected
// component is impacted, the test's own links are not.
func link(ctx context.Context, r Reader, res *Result) error {
	res.Scenarios, res.Policies, res.Evaluators = []Linked{}, []Linked{}, []Linked{}
	if len(res.Affected) == 0 {
		return nil
	}
	byID := map[string]*Affected{}
	ids := make([]string, 0, len(res.Affected))
	for i := range res.Affected {
		a := &res.Affected[i]
		byID[a.Node.ID] = a
		ids = append(ids, a.Node.ID)
	}
	edges, err := r.Edges(ctx, dedupe(ids), true, linkEdges)
	if err != nil {
		return err
	}
	// An agent version's tests are its agent's: follow VERSION_OF once.
	agentOf := map[string][]*Affected{}
	var agentIDs []string
	type attach struct {
		via  *Affected
		edge Edge
	}
	var attached []attach
	for _, e := range edges {
		via := byID[e.From]
		if via == nil {
			continue
		}
		switch {
		case e.Type == EdgeVersionOf && via.Ref.Kind == KindAgentVersion:
			agentOf[e.To] = append(agentOf[e.To], via)
			agentIDs = append(agentIDs, e.To)
		case e.Type != EdgeVersionOf:
			attached = append(attached, attach{via, e})
		}
	}
	agentNodes := map[string]Node{}
	if len(agentIDs) > 0 {
		agentEdges, err := r.Edges(ctx, dedupe(agentIDs), true, []EdgeType{EdgeTestedBy})
		if err != nil {
			return err
		}
		if agentNodes, err = r.Nodes(ctx, dedupe(agentIDs)); err != nil {
			return err
		}
		for _, e := range agentEdges {
			for _, v := range agentOf[e.From] {
				attached = append(attached, attach{v, e})
			}
		}
	}
	targets := make([]string, 0, len(attached))
	for _, a := range attached {
		targets = append(targets, a.edge.To)
	}
	nodes, err := r.Nodes(ctx, dedupe(targets))
	if err != nil {
		return err
	}
	found := map[string]*Linked{}
	var order []string
	for _, a := range attached {
		n, ok := nodes[a.edge.To]
		if !ok || n.ID == a.via.Node.ID || linkTarget[a.edge.Type] != n.Kind {
			continue
		}
		path := slices.Clone(a.via.Path)
		if agent, ok := agentNodes[a.edge.From]; ok {
			path = append(path, Step{Node: agent.Ref(), Label: agent.Label, Edge: EdgeVersionOf, Direction: "down", Confidence: 1, Sources: []Source{SourceManifest}})
		}
		path = append(path, Step{Node: n.Ref(), Label: n.Label, Edge: a.edge.Type, Direction: "down", Confidence: a.edge.Confidence, Sources: a.edge.Sources})
		reason := Reason{Via: a.via.Ref, Label: a.via.Label, Direct: a.via.Direct, Score: a.via.Score, Path: path}
		if agent, ok := agentNodes[a.edge.From]; ok {
			// A test of the whole agent is linked to the change only through
			// the agent: weaker than a test of an affected tool.
			reason.Via, reason.Label = agent.Ref(), agent.Label
			reason.Direct = false
			reason.Score = round3(a.via.Score * indirectFactor)
		}
		l := found[n.ID]
		if l == nil {
			l = &Linked{Ref: n.Ref(), Label: n.Label, Severity: n.Str("severity")}
			found[n.ID] = l
			order = append(order, n.ID)
		}
		if !slices.ContainsFunc(l.Reasons, func(x Reason) bool { return x.Via == reason.Via }) {
			l.Reasons = append(l.Reasons, reason)
		}
	}
	for _, id := range order {
		l := found[id]
		slices.SortFunc(l.Reasons, func(a, b Reason) int {
			if a.Direct != b.Direct {
				if a.Direct {
					return -1
				}
				return 1
			}
			if c := cmp.Compare(b.Score, a.Score); c != 0 {
				return c
			}
			return strings.Compare(a.Via.String(), b.Via.String())
		})
		top := 0.0
		for _, r := range l.Reasons {
			top = max(top, r.Score)
		}
		factor := 1.0
		if l.Ref.Kind == KindScenario {
			if f, ok := scenarioWeight[l.Severity]; ok {
				factor = f
			} else {
				factor = 0.7
			}
		}
		l.Weight = round3(top * factor)
		switch l.Ref.Kind {
		case KindScenario:
			res.Scenarios = append(res.Scenarios, *l)
		case KindPolicy:
			res.Policies = append(res.Policies, *l)
		case KindEvaluator:
			res.Evaluators = append(res.Evaluators, *l)
		}
	}
	for _, list := range []*[]Linked{&res.Scenarios, &res.Policies, &res.Evaluators} {
		slices.SortFunc(*list, func(a, b Linked) int {
			if c := cmp.Compare(b.Weight, a.Weight); c != 0 {
				return c
			}
			return strings.Compare(a.Ref.Key, b.Ref.Key)
		})
	}
	return nil
}
