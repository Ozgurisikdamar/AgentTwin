// Package domain holds control-plane domain logic that is independent of HTTP
// and storage: agent manifests, change sets, release gate rules.
package domain

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"gopkg.in/yaml.v3"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/jsonschemax"
	scenarioschema "github.com/Ozgurisikdamar/AgentTwin/packages/scenario-schema"
)

// MaxManifestBytes bounds manifest documents (YAML/JSON).
const MaxManifestBytes = 512 << 10

// RiskLevel is a tool risk tier.
type RiskLevel string

const (
	RiskRead              RiskLevel = "READ"
	RiskWriteReversible   RiskLevel = "WRITE_REVERSIBLE"
	RiskWriteIrreversible RiskLevel = "WRITE_IRREVERSIBLE"
	RiskExecute           RiskLevel = "EXECUTE"
	RiskAdmin             RiskLevel = "ADMIN"
)

// DefaultRisk is applied to tools declared without a risk: unknown tools are
// treated conservatively (spec §81).
const DefaultRisk = RiskWriteIrreversible

// RiskRank orders tiers by potential damage.
func RiskRank(r RiskLevel) int {
	switch r {
	case RiskRead:
		return 0
	case RiskWriteReversible:
		return 1
	case RiskWriteIrreversible:
		return 2
	case RiskExecute:
		return 3
	case RiskAdmin:
		return 4
	}
	return 2
}

// Tool is a normalized tool reference from a manifest.
type Tool struct {
	Name          string            `json:"name"`
	Version       string            `json:"version,omitempty"`
	Description   string            `json:"description,omitempty"`
	Risk          RiskLevel         `json:"risk"`
	RiskDeclared  bool              `json:"risk_declared"`
	Dimensions    map[string]string `json:"dimensions,omitempty"`
	Compensating  map[string]any    `json:"compensating_action,omitempty"`
	ApprovalWhen  string            `json:"approval_required_when,omitempty"`
	InputSchema   map[string]any    `json:"input_schema,omitempty"`
	DefinitionSHA string            `json:"definition_sha256"`
}

// Model is the model configuration.
type Model struct {
	Provider    string         `json:"provider"`
	Name        string         `json:"name"`
	Temperature *float64       `json:"temperature,omitempty"`
	MaxTokens   *int           `json:"max_tokens,omitempty"`
	Params      map[string]any `json:"params,omitempty"`
}

// Limits are the declared execution limits.
type Limits struct {
	MaxSteps           *int     `json:"max_steps,omitempty"`
	MaxToolCalls       *int     `json:"max_tool_calls,omitempty"`
	MaxDurationSeconds *float64 `json:"max_duration_seconds,omitempty"`
	MaxCostUSD         *float64 `json:"max_cost_usd,omitempty"`
}

// Dependency is a manual tool → system mapping.
type Dependency struct {
	Tool     string `json:"tool"`
	Kind     string `json:"kind"`
	Name     string `json:"name"`
	Relation string `json:"relation"`
	Critical string `json:"criticality,omitempty"`
}

// Manifest is a validated, normalized agent manifest.
type Manifest struct {
	Name             string            `json:"name"`
	Version          string            `json:"version"`
	Description      string            `json:"description,omitempty"`
	Labels           map[string]string `json:"labels,omitempty"`
	Instructions     string            `json:"-"`
	PromptSHA256     string            `json:"prompt_sha256,omitempty"`
	PromptName       string            `json:"prompt_name,omitempty"`
	Model            Model             `json:"model"`
	Limits           Limits            `json:"limits"`
	Tools            []Tool            `json:"tools"`
	RetrievalSources []string          `json:"retrieval_sources,omitempty"`
	ContentMode      string            `json:"content_mode"`
	PIIMode          string            `json:"pii_mode,omitempty"`
	RuntimeEndpoint  string            `json:"runtime_endpoint,omitempty"`
	ExpectedOutcomes []string          `json:"expected_outcomes,omitempty"`
	Dependencies     []Dependency      `json:"dependencies,omitempty"`
}

var (
	manifestSchema     *jsonschema.Schema
	manifestSchemaErr  error
	manifestSchemaOnce sync.Once
)

func schema() (*jsonschema.Schema, error) {
	manifestSchemaOnce.Do(func() {
		manifestSchema, manifestSchemaErr = jsonschemax.Compile(scenarioschema.Schemas, "schemas/agent-manifest.v1.schema.json")
	})
	return manifestSchema, manifestSchemaErr
}

// ManifestError is a user-facing validation failure.
type ManifestError struct {
	Problems []jsonschemax.FieldError
}

func (e *ManifestError) Error() string {
	parts := make([]string, 0, len(e.Problems))
	for _, p := range e.Problems {
		parts = append(parts, p.Path+": "+p.Message)
	}
	return "invalid agent manifest: " + strings.Join(parts, "; ")
}

func problem(path, msg string) *ManifestError {
	return &ManifestError{Problems: []jsonschemax.FieldError{{Path: path, Message: msg}}}
}

// DecodeDocument parses YAML or JSON into JSON-compatible values. YAML is
// parsed in safe mode (yaml.v3 never instantiates arbitrary types); size is
// capped; duplicate keys and non-string keys are rejected.
func DecodeDocument(data []byte) (map[string]any, error) {
	return DecodeDocumentLimit(data, MaxManifestBytes)
}

// DecodeDocumentLimit is DecodeDocument with another size bound (an API
// description is larger than a manifest).
func DecodeDocumentLimit(data []byte, maxBytes int) (map[string]any, error) {
	if len(data) == 0 {
		return nil, problem("/", "document is empty")
	}
	if len(data) > maxBytes {
		return nil, problem("/", fmt.Sprintf("document exceeds %d bytes", maxBytes))
	}
	trimmed := bytes.TrimSpace(data)
	var out any
	if len(trimmed) > 0 && trimmed[0] == '{' {
		dec := json.NewDecoder(bytes.NewReader(trimmed))
		dec.UseNumber()
		if err := dec.Decode(&out); err != nil {
			return nil, problem("/", "invalid JSON: "+err.Error())
		}
	} else {
		var node yaml.Node
		if err := yaml.Unmarshal(data, &node); err != nil {
			return nil, problem("/", "invalid YAML: "+err.Error())
		}
		if err := checkYAMLDepth(&node, 0); err != nil {
			return nil, problem("/", err.Error())
		}
		var v any
		if err := node.Decode(&v); err != nil {
			return nil, problem("/", "invalid YAML: "+err.Error())
		}
		conv, err := toJSONCompatible(v)
		if err != nil {
			return nil, problem("/", err.Error())
		}
		// Round-trip through JSON so numbers become json.Number consistently.
		raw, err := json.Marshal(conv)
		if err != nil {
			return nil, problem("/", err.Error())
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		if err := dec.Decode(&out); err != nil {
			return nil, problem("/", err.Error())
		}
	}
	m, ok := out.(map[string]any)
	if !ok {
		return nil, problem("/", "document must be an object")
	}
	return m, nil
}

const maxYAMLDepth = 64

func checkYAMLDepth(n *yaml.Node, depth int) error {
	if depth > maxYAMLDepth {
		return fmt.Errorf("document nesting exceeds %d levels", maxYAMLDepth)
	}
	if n.Kind == yaml.AliasNode {
		return errors.New("YAML aliases are not allowed")
	}
	for _, c := range n.Content {
		if err := checkYAMLDepth(c, depth+1); err != nil {
			return err
		}
	}
	return nil
}

func toJSONCompatible(v any) (any, error) {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			c, err := toJSONCompatible(val)
			if err != nil {
				return nil, err
			}
			out[k] = c
		}
		return out, nil
	case map[any]any:
		return nil, errors.New("mapping keys must be strings")
	case []any:
		out := make([]any, len(t))
		for i, val := range t {
			c, err := toJSONCompatible(val)
			if err != nil {
				return nil, err
			}
			out[i] = c
		}
		return out, nil
	default:
		return t, nil
	}
}

// ParseManifest decodes, validates and normalizes an agent manifest.
func ParseManifest(data []byte) (*Manifest, error) {
	doc, err := DecodeDocument(data)
	if err != nil {
		return nil, err
	}
	return ManifestFromDocument(doc)
}

// ManifestFromDocument validates and normalizes a decoded manifest document.
func ManifestFromDocument(doc map[string]any) (*Manifest, error) {
	s, err := schema()
	if err != nil {
		return nil, err
	}
	if err := jsonschemax.Validate(s, doc); err != nil {
		var ve *jsonschemax.ValidationError
		if errors.As(err, &ve) {
			return nil, &ManifestError{Problems: ve.Errors}
		}
		return nil, err
	}
	raw, _ := json.Marshal(doc)
	var in struct {
		Metadata struct {
			Name        string            `json:"name"`
			Version     string            `json:"version"`
			Description string            `json:"description"`
			Labels      map[string]string `json:"labels"`
		} `json:"metadata"`
		Spec struct {
			Description  string `json:"description"`
			Instructions string `json:"instructions"`
			PromptRef    *struct {
				Name   string `json:"name"`
				SHA256 string `json:"sha256"`
			} `json:"promptRef"`
			Model  Model `json:"model"`
			Limits struct {
				MaxSteps           *int     `json:"maxSteps"`
				MaxToolCalls       *int     `json:"maxToolCalls"`
				MaxDurationSeconds *float64 `json:"maxDurationSeconds"`
				MaxCostUSD         *float64 `json:"maxCostUsd"`
			} `json:"limits"`
			Tools     []json.RawMessage `json:"tools"`
			Retrieval struct {
				Sources []struct {
					Name string `json:"name"`
				} `json:"sources"`
			} `json:"retrieval"`
			Data struct {
				CaptureContent *bool  `json:"captureContent"`
				ContentMode    string `json:"contentMode"`
				PIIMode        string `json:"piiMode"`
			} `json:"data"`
			Runtime struct {
				Endpoint string `json:"endpoint"`
			} `json:"runtime"`
			ExpectedOutcomes []string `json:"expectedOutcomes"`
			Dependencies     []struct {
				Tool      string `json:"tool"`
				DependsOn []struct {
					Kind        string `json:"kind"`
					Name        string `json:"name"`
					Relation    string `json:"relation"`
					Criticality string `json:"criticality"`
				} `json:"dependsOn"`
			} `json:"dependencies"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(raw, &in); err != nil {
		return nil, problem("/", err.Error())
	}
	m := &Manifest{
		Name:             in.Metadata.Name,
		Version:          in.Metadata.Version,
		Description:      firstNonEmpty(in.Spec.Description, in.Metadata.Description),
		Labels:           in.Metadata.Labels,
		Instructions:     in.Spec.Instructions,
		Model:            in.Spec.Model,
		ExpectedOutcomes: in.Spec.ExpectedOutcomes,
		RuntimeEndpoint:  in.Spec.Runtime.Endpoint,
		PIIMode:          in.Spec.Data.PIIMode,
	}
	m.Limits = Limits{MaxSteps: in.Spec.Limits.MaxSteps, MaxToolCalls: in.Spec.Limits.MaxToolCalls,
		MaxDurationSeconds: in.Spec.Limits.MaxDurationSeconds, MaxCostUSD: in.Spec.Limits.MaxCostUSD}
	switch {
	case in.Spec.Instructions != "" && in.Spec.PromptRef != nil:
		return nil, problem("/spec", "set either instructions or promptRef, not both")
	case in.Spec.Instructions != "":
		m.PromptSHA256 = hashx.SHA256Hex([]byte(in.Spec.Instructions))
	case in.Spec.PromptRef != nil:
		m.PromptSHA256 = in.Spec.PromptRef.SHA256
		m.PromptName = in.Spec.PromptRef.Name
	}
	// Content capture: explicit mode wins, then the boolean; default off (ADR-0008).
	m.ContentMode = "off"
	if in.Spec.Data.CaptureContent != nil && *in.Spec.Data.CaptureContent {
		m.ContentMode = "redacted"
	}
	if in.Spec.Data.ContentMode != "" {
		m.ContentMode = in.Spec.Data.ContentMode
	}
	seen := map[string]bool{}
	for i, rawTool := range in.Spec.Tools {
		t, err := normalizeTool(rawTool)
		if err != nil {
			return nil, problem(fmt.Sprintf("/spec/tools/%d", i), err.Error())
		}
		if seen[t.Name] {
			return nil, problem(fmt.Sprintf("/spec/tools/%d/name", i), fmt.Sprintf("duplicate tool %q", t.Name))
		}
		seen[t.Name] = true
		m.Tools = append(m.Tools, t)
	}
	sort.Slice(m.Tools, func(i, j int) bool { return m.Tools[i].Name < m.Tools[j].Name })
	for _, s := range in.Spec.Retrieval.Sources {
		m.RetrievalSources = append(m.RetrievalSources, s.Name)
	}
	sort.Strings(m.RetrievalSources)
	for i, d := range in.Spec.Dependencies {
		if !seen[d.Tool] {
			return nil, problem(fmt.Sprintf("/spec/dependencies/%d/tool", i), fmt.Sprintf("dependency references undeclared tool %q", d.Tool))
		}
		for _, on := range d.DependsOn {
			rel := on.Relation
			if rel == "" {
				rel = "DEPENDS_ON"
			}
			m.Dependencies = append(m.Dependencies, Dependency{Tool: d.Tool, Kind: on.Kind, Name: on.Name, Relation: rel, Critical: on.Criticality})
		}
	}
	return m, nil
}

func normalizeTool(raw json.RawMessage) (Tool, error) {
	var in struct {
		Name        string          `json:"name"`
		Version     string          `json:"version"`
		Description string          `json:"description"`
		Risk        json.RawMessage `json:"risk"`
		Approval    *struct {
			RequiredWhen string `json:"requiredWhen"`
		} `json:"approval"`
		InputSchema map[string]any `json:"inputSchema"`
	}
	if err := json.Unmarshal(raw, &in); err != nil {
		return Tool{}, err
	}
	t := Tool{Name: in.Name, Version: in.Version, Description: in.Description, InputSchema: in.InputSchema, Risk: DefaultRisk}
	if in.Approval != nil {
		t.ApprovalWhen = strings.TrimSpace(in.Approval.RequiredWhen)
	}
	if len(in.Risk) > 0 && string(in.Risk) != "null" {
		t.RiskDeclared = true
		var level string
		if err := json.Unmarshal(in.Risk, &level); err == nil {
			t.Risk = RiskLevel(level)
		} else {
			var obj struct {
				Level              RiskLevel         `json:"level"`
				Dimensions         map[string]string `json:"dimensions"`
				CompensatingAction map[string]any    `json:"compensatingAction"`
			}
			if err := json.Unmarshal(in.Risk, &obj); err != nil {
				return Tool{}, err
			}
			t.Risk, t.Dimensions, t.Compensating = obj.Level, obj.Dimensions, obj.CompensatingAction
		}
	}
	t.DefinitionSHA = hashx.MustOf(map[string]any{
		"risk": t.Risk, "dimensions": t.Dimensions, "compensating": t.Compensating,
		"approval": t.ApprovalWhen, "input_schema": t.InputSchema, "description": t.Description, "version": t.Version,
	})
	return t, nil
}

// Hash is the content identity of the manifest. The prompt participates
// through its hash so prompt changes change the manifest identity even when
// prompt text is not stored.
func (m *Manifest) Hash() string {
	return hashx.MustOf(m)
}

// Tool returns the named tool.
func (m *Manifest) Tool(name string) (Tool, bool) {
	for _, t := range m.Tools {
		if t.Name == name {
			return t, true
		}
	}
	return Tool{}, false
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
