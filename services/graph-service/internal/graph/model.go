// Package graph holds the dependency graph's vocabulary and the bounded
// blast-radius traversal. It has no I/O: the traversal reads the graph
// through the Reader interface, one breadth-first level at a time.
package graph

import (
	"fmt"
	"regexp"
	"slices"
)

// Kind is a component (node) kind (spec §19).
type Kind string

// Component kinds. RETRIEVAL_SOURCE is added to the specification's minimum
// set: a knowledge base the agent reads is neither a service it calls nor a
// dataset it is tested on.
const (
	KindAgent        Kind = "AGENT"
	KindAgentVersion Kind = "AGENT_VERSION"
	KindPrompt       Kind = "PROMPT"
	KindModel        Kind = "MODEL"
	KindTool         Kind = "TOOL"
	KindMCPServer    Kind = "MCP_SERVER"
	KindHTTPAPI      Kind = "HTTP_API"
	KindService      Kind = "SERVICE"
	KindDatabase     Kind = "DATABASE"
	KindQueue        Kind = "QUEUE"
	KindDataset      Kind = "DATASET"
	KindPolicy       Kind = "POLICY"
	KindScenario     Kind = "SCENARIO"
	KindEvaluator    Kind = "EVALUATOR"
	KindExternal     Kind = "EXTERNAL_SYSTEM"
	KindRetrieval    Kind = "RETRIEVAL_SOURCE"
)

// Kinds lists every kind in display order.
var Kinds = []Kind{
	KindAgent, KindAgentVersion, KindPrompt, KindModel, KindTool, KindMCPServer, KindHTTPAPI,
	KindService, KindDatabase, KindQueue, KindExternal, KindRetrieval, KindDataset, KindPolicy,
	KindScenario, KindEvaluator,
}

// Valid reports whether k is a known kind.
func (k Kind) Valid() bool { return slices.Contains(Kinds, k) }

// DependencyKinds are the kinds a manifest or a manual mapping may name as
// something a tool depends on.
var DependencyKinds = []Kind{KindService, KindDatabase, KindQueue, KindExternal, KindHTTPAPI, KindMCPServer}

// EdgeType is a relationship (spec §19). VERSION_OF is added: it groups an
// agent's versions under the agent without making them depend on each other.
type EdgeType string

// Edge types. An edge reads "from TYPE to": an agent version USES a tool, a
// tool WRITES a database, a tool is TESTED_BY a scenario.
const (
	EdgeUses          EdgeType = "USES"
	EdgeCalls         EdgeType = "CALLS"
	EdgeReads         EdgeType = "READS"
	EdgeWrites        EdgeType = "WRITES"
	EdgePublishes     EdgeType = "PUBLISHES"
	EdgeConsumes      EdgeType = "CONSUMES"
	EdgeGuardedBy     EdgeType = "GUARDED_BY"
	EdgeEvaluatedBy   EdgeType = "EVALUATED_BY"
	EdgeTestedBy      EdgeType = "TESTED_BY"
	EdgeDependsOn     EdgeType = "DEPENDS_ON"
	EdgeRetrievesFrom EdgeType = "RETRIEVES_FROM"
	EdgeCanMutate     EdgeType = "CAN_MUTATE"
	EdgeVersionOf     EdgeType = "VERSION_OF"
)

// EdgeTypes lists every edge type.
var EdgeTypes = []EdgeType{
	EdgeUses, EdgeCalls, EdgeReads, EdgeWrites, EdgePublishes, EdgeConsumes, EdgeGuardedBy,
	EdgeEvaluatedBy, EdgeTestedBy, EdgeDependsOn, EdgeRetrievesFrom, EdgeCanMutate, EdgeVersionOf,
}

// Valid reports whether t is a known edge type.
func (t EdgeType) Valid() bool { return slices.Contains(EdgeTypes, t) }

// DependencyRelations are the relations a declared dependency may use.
var DependencyRelations = []EdgeType{EdgeCalls, EdgeReads, EdgeWrites, EdgePublishes, EdgeConsumes, EdgeDependsOn, EdgeCanMutate}

// Source says why an edge exists (spec §19 "edge evidence").
type Source string

// Evidence sources. SCENARIO is a scenario document naming what it covers:
// declared by its author, like a manifest.
const (
	SourceManifest Source = "MANIFEST"
	SourceOpenAPI  Source = "OPENAPI"
	SourceMCP      Source = "MCP"
	SourceObserved Source = "OBSERVED"
	SourceManual   Source = "MANUAL"
	SourceScenario Source = "SCENARIO"
	SourceInferred Source = "INFERRED"
)

// Sources lists every source.
var Sources = []Source{SourceManifest, SourceOpenAPI, SourceMCP, SourceObserved, SourceManual, SourceScenario, SourceInferred}

// Valid reports whether s is a known source.
func (s Source) Valid() bool { return slices.Contains(Sources, s) }

// Confidence is how far an edge from this source can be trusted to exist.
// Declared and observed relationships are facts; an import describes an
// interface, not that the agent uses it that way; an MCP server's own tool
// list is untrusted input (spec §33); an inference is never certain.
func (s Source) Confidence() float64 {
	switch s {
	case SourceManifest, SourceManual, SourceScenario, SourceObserved:
		return 1.0
	case SourceOpenAPI:
		return 0.9
	case SourceMCP:
		return 0.8
	default:
		return 0.5
	}
}

// Certain reports whether an edge of this source may be shown as certain.
// Inferred relationships never are (spec §19).
func (s Source) Certain() bool { return s != SourceInferred }

// Ref names a component within a project.
type Ref struct {
	Kind Kind   `json:"kind"`
	Key  string `json:"key"`
}

func (r Ref) String() string { return string(r.Kind) + ":" + r.Key }

var keyPattern = regexp.MustCompile(`^[^\x00-\x1f]{1,300}$`)

// Validate checks a reference supplied by a caller.
func (r Ref) Validate() error {
	if !r.Kind.Valid() {
		return fmt.Errorf("unknown component kind %q", r.Kind)
	}
	if !keyPattern.MatchString(r.Key) {
		return fmt.Errorf("component key must be 1 to 300 printable characters")
	}
	return nil
}

// Node is a component as the traversal sees it.
type Node struct {
	ID    string         `json:"id"`
	Kind  Kind           `json:"kind"`
	Key   string         `json:"key"`
	Label string         `json:"label"`
	Attrs map[string]any `json:"attributes"`
}

// Ref returns the node's reference.
func (n Node) Ref() Ref { return Ref{Kind: n.Kind, Key: n.Key} }

// Str returns a string attribute or "".
func (n Node) Str(name string) string {
	if v, ok := n.Attrs[name].(string); ok {
		return v
	}
	return ""
}

// Edge is a relationship with its combined evidence.
type Edge struct {
	ID         string   `json:"id"`
	From       string   `json:"from"`
	To         string   `json:"to"`
	Type       EdgeType `json:"type"`
	Confidence float64  `json:"confidence"`
	Sources    []Source `json:"sources"`
}

// Certain reports whether any of the edge's evidence is certain.
func (e Edge) Certain() bool {
	for _, s := range e.Sources {
		if s.Certain() {
			return true
		}
	}
	return false
}

// CombinedConfidence is the confidence of an edge supported by several
// sources: the strongest one.
func CombinedConfidence(sources []Source) float64 {
	best := 0.0
	for _, s := range sources {
		best = max(best, s.Confidence())
	}
	return best
}
