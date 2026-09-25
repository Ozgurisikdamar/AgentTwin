// Package ingest turns the events the graph listens to into facts: the
// components they name, the relationships between them and the evidence for
// each. It is pure; the store applies facts.
package ingest

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/graph"
)

// NodeFact asserts a component. Attributes are merged into what is known.
type NodeFact struct {
	Ref   graph.Ref
	Label string
	Attrs map[string]any
}

// EdgeFact asserts a relationship with one piece of evidence.
type EdgeFact struct {
	From, To     graph.Ref
	Type         graph.EdgeType
	Source       graph.Source
	SourceRef    string
	Detail       map[string]any
	Observations int64
}

// Replace says that the evidence of one source and reference is exactly
// what the facts assert: anything it asserted before and no longer does is
// removed (a scenario that stops covering a tool, an import that drops an
// operation, a policy retargeted).
type Replace struct {
	Source    graph.Source
	SourceRef string
}

// Facts is what one event, import or mapping establishes.
type Facts struct {
	Nodes    []NodeFact
	Edges    []EdgeFact
	Replaces []Replace
	// LatestOf names the agents whose latest version must be recomputed.
	LatestOf []string
}

func (f *Facts) node(kind graph.Kind, key, label string, attrs map[string]any) graph.Ref {
	r := graph.Ref{Kind: kind, Key: key}
	if label == "" {
		label = key
	}
	if attrs == nil {
		attrs = map[string]any{}
	}
	f.Nodes = append(f.Nodes, NodeFact{Ref: r, Label: label, Attrs: attrs})
	return r
}

func (f *Facts) edge(from graph.Ref, t graph.EdgeType, to graph.Ref, src graph.Source, ref string, detail map[string]any) {
	if detail == nil {
		detail = map[string]any{}
	}
	f.Edges = append(f.Edges, EdgeFact{From: from, To: to, Type: t, Source: src, SourceRef: ref, Detail: detail, Observations: 1})
}

// Validate checks every reference and relation the facts name.
func (f Facts) Validate() error {
	for _, n := range f.Nodes {
		if err := n.Ref.Validate(); err != nil {
			return err
		}
	}
	for _, e := range f.Edges {
		if err := e.From.Validate(); err != nil {
			return err
		}
		if err := e.To.Validate(); err != nil {
			return err
		}
		if !e.Type.Valid() || !e.Source.Valid() || e.SourceRef == "" || len(e.SourceRef) > 300 {
			return fmt.Errorf("invalid relationship %s -%s-> %s", e.From, e.Type, e.To)
		}
	}
	return nil
}

// AgentVersion is the payload of agent.version_registered.v1.
type AgentVersion struct {
	AgentName    string  `json:"agent_name"`
	VersionID    string  `json:"version_id"`
	Version      string  `json:"version"`
	ManifestHash string  `json:"manifest_hash"`
	PromptHash   *string `json:"prompt_hash"`
	Model        *struct {
		Provider string `json:"provider"`
		Name     string `json:"name"`
	} `json:"model"`
	Tools []struct {
		Name string `json:"name"`
		Risk string `json:"risk"`
	} `json:"tools"`
	RetrievalSources []string     `json:"retrieval_sources"`
	Dependencies     []Dependency `json:"dependencies"`
}

// Dependency is a declared tool → system relationship.
type Dependency struct {
	Tool        string `json:"tool"`
	Kind        string `json:"kind"`
	Name        string `json:"name"`
	Relation    string `json:"relation"`
	Criticality string `json:"criticality"`
}

// FromAgentVersion maps a registered agent version: its prompt, model,
// tools, retrieval sources and declared dependencies, all declared by its
// manifest.
func FromAgentVersion(payload json.RawMessage, at time.Time) (Facts, error) {
	var p AgentVersion
	if err := json.Unmarshal(payload, &p); err != nil {
		return Facts{}, fmt.Errorf("agent.version_registered.v1: %w", err)
	}
	if p.AgentName == "" || p.Version == "" {
		return Facts{}, fmt.Errorf("agent.version_registered.v1: agent_name and version are required")
	}
	var f Facts
	ref := "manifest:" + p.AgentName + "@" + p.Version
	agent := f.node(graph.KindAgent, p.AgentName, "", nil)
	version := f.node(graph.KindAgentVersion, p.AgentName+"@"+p.Version, "", map[string]any{
		"agent": p.AgentName, "version": p.Version, "version_id": p.VersionID,
		"manifest_hash": p.ManifestHash, "registered_at": at.UTC().Format(time.RFC3339Nano),
	})
	f.edge(version, graph.EdgeVersionOf, agent, graph.SourceManifest, ref, nil)
	if p.PromptHash != nil && *p.PromptHash != "" {
		h := *p.PromptHash
		f.edge(version, graph.EdgeUses, f.node(graph.KindPrompt, h, "prompt "+short(h), map[string]any{"sha256": h}),
			graph.SourceManifest, ref, nil)
	}
	if p.Model != nil && p.Model.Name != "" {
		key := p.Model.Provider + "/" + p.Model.Name
		f.edge(version, graph.EdgeUses, f.node(graph.KindModel, key, "", map[string]any{"provider": p.Model.Provider, "name": p.Model.Name}),
			graph.SourceManifest, ref, nil)
	}
	for _, t := range p.Tools {
		f.edge(version, graph.EdgeUses, f.node(graph.KindTool, t.Name, "", map[string]any{"risk": t.Risk}),
			graph.SourceManifest, ref, map[string]any{"risk": t.Risk})
	}
	for _, s := range p.RetrievalSources {
		f.edge(version, graph.EdgeRetrievesFrom, f.node(graph.KindRetrieval, s, "", nil), graph.SourceManifest, ref, nil)
	}
	for _, d := range p.Dependencies {
		to, rel, err := dependency(d)
		if err != nil {
			return Facts{}, fmt.Errorf("agent.version_registered.v1: %w", err)
		}
		f.Nodes = append(f.Nodes, to)
		f.edge(graph.Ref{Kind: graph.KindTool, Key: d.Tool}, rel, to.Ref, graph.SourceManifest, ref, nil)
	}
	f.LatestOf = []string{p.AgentName}
	return f, f.Validate()
}

// dependency maps a declared dependency to its component and relation.
func dependency(d Dependency) (NodeFact, graph.EdgeType, error) {
	kind := graph.Kind(strings.ToUpper(d.Kind))
	if !contains(graph.DependencyKinds, kind) {
		return NodeFact{}, "", fmt.Errorf("dependency kind must be one of %v, got %q", graph.DependencyKinds, d.Kind)
	}
	rel := graph.EdgeType(strings.ToUpper(d.Relation))
	if rel == "" {
		rel = graph.EdgeDependsOn
	}
	if !contains(graph.DependencyRelations, rel) {
		return NodeFact{}, "", fmt.Errorf("dependency relation must be one of %v, got %q", graph.DependencyRelations, d.Relation)
	}
	attrs := map[string]any{}
	if c := strings.ToUpper(d.Criticality); c != "" {
		if !contains([]string{"LOW", "MEDIUM", "HIGH", "CRITICAL"}, c) {
			return NodeFact{}, "", fmt.Errorf("criticality must be LOW, MEDIUM, HIGH or CRITICAL, got %q", d.Criticality)
		}
		attrs["criticality"] = c
	}
	return NodeFact{Ref: graph.Ref{Kind: kind, Key: d.Name}, Label: d.Name, Attrs: attrs}, rel, nil
}

// ToolCatalog is the payload of tool.catalog_imported.v1.
type ToolCatalog struct {
	Source     string  `json:"source"`
	SourceName string  `json:"source_name"`
	Service    *string `json:"service"`
	Tools      []struct {
		Name     string  `json:"name"`
		Risk     string  `json:"risk"`
		Method   *string `json:"method"`
		Path     *string `json:"path"`
		Mutating bool    `json:"mutating"`
	} `json:"tools"`
}

// FromToolCatalog maps an imported tool catalog. An OpenAPI document is an
// HTTP API whose operations are tools (a mutating operation CAN_MUTATE it);
// an MCP server's tools call the server. The import describes interfaces:
// its risk is kept apart from a manifest's (imported_risk). Each import
// replaces the previous one of the same source and name.
func FromToolCatalog(payload json.RawMessage) (Facts, error) {
	var p ToolCatalog
	if err := json.Unmarshal(payload, &p); err != nil {
		return Facts{}, fmt.Errorf("tool.catalog_imported.v1: %w", err)
	}
	var f Facts
	switch p.Source {
	case "OPENAPI":
		ref := "openapi:" + p.SourceName
		api := f.node(graph.KindHTTPAPI, p.SourceName, "", nil)
		if p.Service != nil && *p.Service != "" {
			f.edge(api, graph.EdgeDependsOn, f.node(graph.KindService, *p.Service, "", nil), graph.SourceOpenAPI, ref, nil)
		}
		for _, t := range p.Tools {
			rel := graph.EdgeCalls
			if t.Mutating {
				rel = graph.EdgeCanMutate
			}
			detail := map[string]any{"mutating": t.Mutating}
			if t.Method != nil {
				detail["method"] = *t.Method
			}
			if t.Path != nil {
				detail["path"] = *t.Path
			}
			f.edge(f.node(graph.KindTool, t.Name, "", map[string]any{"imported_risk": t.Risk}), rel, api, graph.SourceOpenAPI, ref, detail)
		}
		f.Replaces = []Replace{{graph.SourceOpenAPI, ref}}
	case "MCP":
		ref := "mcp:" + p.SourceName
		server := f.node(graph.KindMCPServer, p.SourceName, "", nil)
		for _, t := range p.Tools {
			f.edge(f.node(graph.KindTool, t.Name, "", map[string]any{"imported_risk": t.Risk}), graph.EdgeCalls, server,
				graph.SourceMCP, ref, map[string]any{"mutating": t.Mutating})
		}
		f.Replaces = []Replace{{graph.SourceMCP, ref}}
	case "MANUAL":
		for _, t := range p.Tools {
			f.node(graph.KindTool, t.Name, "", map[string]any{"risk": t.Risk})
		}
	default:
		return Facts{}, fmt.Errorf("tool.catalog_imported.v1: unknown source %q", p.Source)
	}
	return f, f.Validate()
}

// Trace is the part of trace.ingested.v1 the graph reads.
type Trace struct {
	TraceID       string  `json:"trace_id"`
	Agent         string  `json:"agent"`
	AgentVersion  *string `json:"agent_version"`
	Environment   string  `json:"environment"`
	Source        string  `json:"source"`
	ObservedTools []struct {
		Name     string  `json:"name"`
		Risk     *string `json:"risk"`
		HTTPHost *string `json:"http_host"`
		Count    int64   `json:"count"`
	} `json:"observed_tools"`
}

// FromTrace maps the tools a production trace actually called: observed
// edges, counted rather than recorded per trace. Traces of simulations,
// replays and tests are not production usage and are ignored (their tools
// call twins, not the real systems).
func FromTrace(payload json.RawMessage) (Facts, error) {
	var p Trace
	if err := json.Unmarshal(payload, &p); err != nil {
		return Facts{}, fmt.Errorf("trace.ingested.v1: %w", err)
	}
	var f Facts
	if (p.Source != "" && p.Source != "production") || p.Agent == "" || len(p.ObservedTools) == 0 {
		return f, nil
	}
	caller := f.node(graph.KindAgent, p.Agent, "", nil)
	if p.AgentVersion != nil && *p.AgentVersion != "" {
		version := f.node(graph.KindAgentVersion, p.Agent+"@"+*p.AgentVersion, "", map[string]any{"agent": p.Agent, "version": *p.AgentVersion})
		f.edge(version, graph.EdgeVersionOf, caller, graph.SourceObserved, "traces", nil)
		caller = version
	}
	for _, t := range p.ObservedTools {
		if t.Name == "" {
			continue
		}
		n := max(t.Count, 1)
		detail := map[string]any{"last_trace_id": p.TraceID, "environment": p.Environment}
		tool := f.node(graph.KindTool, t.Name, "", nil)
		f.Edges = append(f.Edges, EdgeFact{From: caller, To: tool, Type: graph.EdgeUses, Source: graph.SourceObserved,
			SourceRef: "traces", Detail: detail, Observations: n})
		if t.HTTPHost != nil && *t.HTTPHost != "" {
			host := f.node(graph.KindHTTPAPI, *t.HTTPHost, "", map[string]any{"observed_host": true})
			f.Edges = append(f.Edges, EdgeFact{From: tool, To: host, Type: graph.EdgeCalls, Source: graph.SourceObserved,
				SourceRef: "traces", Detail: detail, Observations: n})
		}
	}
	return f, f.Validate()
}

// Scenario is the payload of scenario.upserted.v1.
type Scenario struct {
	ScenarioID        string   `json:"scenario_id"`
	ScenarioVersionID string   `json:"scenario_version_id"`
	Name              string   `json:"name"`
	Agent             *string  `json:"agent"`
	Severity          string   `json:"severity"`
	Tags              []string `json:"tags"`
	Covers            []string `json:"covers"`
	SpecHash          string   `json:"spec_hash"`
}

// coverKinds are the covers entries that name components; the others
// (faults, threats) describe the scenario and are kept as its attributes.
var coverKinds = map[string]graph.Kind{
	"tool": graph.KindTool, "retrieval": graph.KindRetrieval, "policy": graph.KindPolicy,
	"service": graph.KindService, "database": graph.KindDatabase, "api": graph.KindHTTPAPI, "mcp": graph.KindMCPServer,
}

// FromScenario maps a scenario version: it tests its agent and every
// component it says it covers. Each version replaces what the previous one
// declared.
func FromScenario(payload json.RawMessage) (Facts, error) {
	var p Scenario
	if err := json.Unmarshal(payload, &p); err != nil {
		return Facts{}, fmt.Errorf("scenario.upserted.v1: %w", err)
	}
	if p.Name == "" {
		return Facts{}, fmt.Errorf("scenario.upserted.v1: name is required")
	}
	var f Facts
	ref := "scenario:" + p.Name
	type cover struct {
		kind       graph.Kind
		name, text string
	}
	var named []cover
	other := []string{}
	for _, c := range p.Covers {
		prefix, name, _ := strings.Cut(c, ":")
		if kind, known := coverKinds[prefix]; known && name != "" {
			named = append(named, cover{kind, name, c})
			continue
		}
		other = append(other, c)
	}
	tags := p.Tags
	if tags == nil {
		tags = []string{}
	}
	scenario := f.node(graph.KindScenario, p.Name, "", map[string]any{
		"severity": p.Severity, "tags": tags, "covers_other": other,
		"scenario_id": p.ScenarioID, "scenario_version_id": p.ScenarioVersionID, "spec_hash": p.SpecHash,
	})
	if p.Agent != nil && *p.Agent != "" {
		f.edge(f.node(graph.KindAgent, *p.Agent, "", nil), graph.EdgeTestedBy, scenario, graph.SourceScenario, ref, nil)
	}
	for _, c := range named {
		f.edge(f.node(c.kind, c.name, "", nil), graph.EdgeTestedBy, scenario, graph.SourceScenario, ref, map[string]any{"covers": c.text})
	}
	f.Replaces = []Replace{{graph.SourceScenario, ref}}
	return f, f.Validate()
}

// Policy is the payload of policy.activated.v1.
type Policy struct {
	PolicyID   string `json:"policy_id"`
	Name       string `json:"name"`
	Version    int    `json:"version"`
	TargetTool string `json:"target_tool"`
	SpecHash   string `json:"spec_hash"`
}

// FromPolicy maps an activated policy: the tool it guards, declared by the
// policy's author. A new activation replaces the previous target.
func FromPolicy(payload json.RawMessage) (Facts, error) {
	var p Policy
	if err := json.Unmarshal(payload, &p); err != nil {
		return Facts{}, fmt.Errorf("policy.activated.v1: %w", err)
	}
	if p.Name == "" || p.TargetTool == "" {
		return Facts{}, fmt.Errorf("policy.activated.v1: name and target_tool are required")
	}
	var f Facts
	ref := "policy:" + p.Name
	policy := f.node(graph.KindPolicy, p.Name, "", map[string]any{"policy_id": p.PolicyID, "version": p.Version, "spec_hash": p.SpecHash})
	f.edge(f.node(graph.KindTool, p.TargetTool, "", nil), graph.EdgeGuardedBy, policy, graph.SourceManual, ref, nil)
	f.Replaces = []Replace{{graph.SourceManual, ref}}
	return f, f.Validate()
}

// Mapping is a person's declaration of what a tool depends on (spec §20.5).
type Mapping struct {
	DependsOn []struct {
		Kind        string `json:"kind"`
		Name        string `json:"name"`
		Relation    string `json:"relation,omitempty"`
		Criticality string `json:"criticality,omitempty"`
	} `json:"depends_on"`
}

// FromMapping maps a manual mapping of one tool; it replaces the tool's
// previous mapping (an empty one removes it).
func FromMapping(tool string, m Mapping) (Facts, error) {
	var f Facts
	ref := "mapping:" + tool
	from := f.node(graph.KindTool, tool, "", nil)
	for _, d := range m.DependsOn {
		to, rel, err := dependency(Dependency{Tool: tool, Kind: d.Kind, Name: d.Name, Relation: d.Relation, Criticality: d.Criticality})
		if err != nil {
			return Facts{}, err
		}
		f.Nodes = append(f.Nodes, to)
		f.edge(from, rel, to.Ref, graph.SourceManual, ref, nil)
	}
	f.Replaces = []Replace{{graph.SourceManual, ref}}
	return f, f.Validate()
}

func short(h string) string {
	h = strings.TrimPrefix(h, "sha256:")
	if len(h) > 12 {
		return h[:12]
	}
	return h
}

func contains[T comparable](list []T, v T) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
