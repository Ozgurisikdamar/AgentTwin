// Package catalog turns an API's own description of its operations into
// entries of the tool registry (spec §20.2, §20.3, §33): an OpenAPI 3.x
// document, or the tools an MCP server lists. It is pure: nothing is
// fetched. A document is untrusted input, so every size and depth is
// bounded, references outside the document are never followed, and what it
// says about its own risk is recorded as a claim, not a fact.
package catalog

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// Sources of a catalog.
const (
	SourceOpenAPI = "OPENAPI"
	SourceMCP     = "MCP"
)

// Where an entry's risk comes from, strongest first.
const (
	// RiskOverride: the person importing set it.
	RiskOverride = "override"
	// RiskDeclared: the document declares it (x-agenttwin-risk).
	RiskDeclared = "declared"
	// RiskAnnotation: an MCP server's annotations, trusted on request.
	RiskAnnotation = "annotation"
	// RiskInferred: from the HTTP method (GET, HEAD, OPTIONS, TRACE read).
	RiskInferred = "inferred"
	// RiskDefault: nothing is known; an unknown tool is assumed to change
	// something that cannot be undone.
	RiskDefault = "default"
)

// Bounds on what one catalog imports.
const (
	MaxEntries        = 500
	maxDescription    = 2000
	maxSchemaBytes    = 64 << 10
	maxSchemaNodes    = 20000
	maxRefDepth       = 16
	maxSchemaDepth    = 40
	maxWarnings       = 100
	maxServers        = 10
	maxTags           = 20
	maxNameMapEntries = 500
)

// Entry is one tool of a catalog.
type Entry struct {
	// Name is the registry tool name.
	Name string `json:"name"`
	// Operation is what the source calls it: an OpenAPI operationId (or
	// "METHOD /path" without one), an MCP tool name.
	Operation   string           `json:"operation"`
	Method      string           `json:"method,omitempty"`
	Path        string           `json:"path,omitempty"`
	Title       string           `json:"title,omitempty"`
	Description string           `json:"description"`
	Tags        []string         `json:"tags,omitempty"`
	Deprecated  bool             `json:"deprecated,omitempty"`
	Risk        domain.RiskLevel `json:"risk"`
	RiskSource  string           `json:"risk_source"`
	Mutating    bool             `json:"mutating"`
	InputSchema map[string]any   `json:"input_schema"`
	// Hints are an MCP server's annotations as it sent them; HintRisk is
	// the risk they suggest. Both are claims of the server (spec §33).
	Hints    map[string]any   `json:"hints,omitempty"`
	HintRisk domain.RiskLevel `json:"hint_risk,omitempty"`
}

// Skipped is an operation that did not become an entry.
type Skipped struct {
	Operation string `json:"operation"`
	Reason    string `json:"reason"`
}

// Catalog is a normalized document.
type Catalog struct {
	Source string `json:"source"`
	// Title and APIVersion are the document's own (info.title and
	// info.version; the MCP server's name and version).
	Title       string `json:"title"`
	APIVersion  string `json:"api_version,omitempty"`
	SpecVersion string `json:"spec_version,omitempty"`
	// Servers are recorded, never contacted; credentials and query
	// strings are removed.
	Servers  []string  `json:"servers"`
	Entries  []Entry   `json:"entries"`
	Skipped  []Skipped `json:"skipped"`
	Warnings []string  `json:"warnings"`
}

// Options are the importer's decisions.
type Options struct {
	// RiskOverrides maps a tool name or an operation to a risk.
	RiskOverrides map[string]domain.RiskLevel
	// Names maps an operation to the registry name it takes.
	Names map[string]string
	// TrustAnnotations lets an MCP server's annotations set risk.
	TrustAnnotations bool
}

// Error is a document that cannot be imported at all.
type Error struct{ Reason string }

func (e *Error) Error() string { return e.Reason }

func fail(format string, args ...any) error { return &Error{Reason: fmt.Sprintf(format, args...)} }

// ToolNamePattern is the registry's tool name.
var ToolNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// ToolName turns an operation name into a registry name: camelCase and
// dotted names become snake_case ("refundPayment" → "refund_payment",
// "admin.users.list" → "admin_users_list"). It reports false when the
// result is not a valid name (empty, or longer than 63).
func ToolName(s string) (string, bool) {
	var b strings.Builder
	rs := []rune(s)
	for i, r := range rs {
		switch {
		case unicode.IsUpper(r):
			prevLower := i > 0 && (unicode.IsLower(rs[i-1]) || unicode.IsDigit(rs[i-1]))
			nextLower := i > 0 && i+1 < len(rs) && unicode.IsUpper(rs[i-1]) && unicode.IsLower(rs[i+1])
			if prevLower || nextLower {
				b.WriteByte('_')
			}
			b.WriteRune(unicode.ToLower(r))
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	name := b.String()
	for strings.Contains(name, "__") {
		name = strings.ReplaceAll(name, "__", "_")
	}
	name = strings.Trim(name, "_-")
	return name, ToolNamePattern.MatchString(name)
}

// cleanText removes control characters (but newlines and tabs) and bounds
// the length of a document's text.
func cleanText(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' {
			return r
		}
		if unicode.IsControl(r) || r == utf8.RuneError {
			return -1
		}
		return r
	}, s)
	s = strings.TrimSpace(s)
	if r := []rune(s); len(r) > max {
		s = string(r[:max-1]) + "…"
	}
	return s
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

// builder collects entries and applies the options.
type builder struct {
	c         Catalog
	opts      Options
	usedNames map[string]string // name → operation
	usedOpts  map[string]bool   // option keys that matched
}

func newBuilder(source string, opts Options) *builder {
	return &builder{c: Catalog{Source: source, Servers: []string{}, Entries: []Entry{}, Skipped: []Skipped{}, Warnings: []string{}},
		opts: opts, usedNames: map[string]string{}, usedOpts: map[string]bool{}}
}

func (b *builder) warn(format string, args ...any) {
	w := fmt.Sprintf(format, args...)
	for _, x := range b.c.Warnings {
		if x == w {
			return
		}
	}
	if len(b.c.Warnings) < maxWarnings {
		b.c.Warnings = append(b.c.Warnings, w)
	}
}

func (b *builder) skip(op, reason string) {
	b.c.Skipped = append(b.c.Skipped, Skipped{Operation: op, Reason: reason})
}

// name picks the registry name: the importer's mapping, the document's
// own choice, then the operation's name.
func (b *builder) name(op string, keys []string, declared string, fallback string) (string, bool) {
	for _, k := range keys {
		if n, ok := b.opts.Names[k]; ok {
			b.usedOpts["name:"+k] = true
			if !ToolNamePattern.MatchString(n) {
				b.skip(op, fmt.Sprintf("the name %q given for it is not a tool name", n))
				return "", false
			}
			return n, true
		}
	}
	if declared != "" {
		if ToolNamePattern.MatchString(declared) {
			return declared, true
		}
		b.warn("%s: x-agenttwin-tool %q is not a tool name; ignored", op, declared)
	}
	n, ok := ToolName(fallback)
	if !ok {
		b.skip(op, "no valid tool name can be derived from it; name it in names")
		return "", false
	}
	return n, true
}

// override returns the importer's risk for a tool, if any.
func (b *builder) override(keys ...string) (domain.RiskLevel, bool) {
	for _, k := range keys {
		if r, ok := b.opts.RiskOverrides[k]; ok {
			b.usedOpts["risk:"+k] = true
			return r, true
		}
	}
	return "", false
}

func (b *builder) add(e Entry) {
	if other, taken := b.usedNames[e.Name]; taken {
		b.skip(e.Operation, fmt.Sprintf("the name %s is taken by %s; name it in names", e.Name, other))
		return
	}
	if len(b.c.Entries) >= MaxEntries {
		b.skip(e.Operation, fmt.Sprintf("a catalog imports at most %d tools", MaxEntries))
		return
	}
	b.usedNames[e.Name] = e.Operation
	e.Mutating = e.Risk != domain.RiskRead
	b.c.Entries = append(b.c.Entries, e)
}

func (b *builder) finish() Catalog {
	for _, k := range sortedKeys(b.opts.RiskOverrides) {
		if !b.usedOpts["risk:"+k] {
			b.warn("risk_overrides names %q, which is not in the document", k)
		}
	}
	for _, k := range sortedKeys(b.opts.Names) {
		if !b.usedOpts["name:"+k] {
			b.warn("names maps %q, which is not in the document", k)
		}
	}
	sort.SliceStable(b.c.Entries, func(i, j int) bool { return b.c.Entries[i].Name < b.c.Entries[j].Name })
	return b.c
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// boundSchema keeps a schema whose serialized size and node count are
// within bounds; otherwise it is replaced by an open object schema.
func (b *builder) boundSchema(op string, s map[string]any) map[string]any {
	raw, err := json.Marshal(s)
	if err != nil || len(raw) > maxSchemaBytes || countNodes(s, 0) > maxSchemaNodes {
		b.warn("%s: input schema too large to keep; recorded as an open object", op)
		return map[string]any{"type": "object"}
	}
	return s
}

func countNodes(v any, n int) int {
	n++
	switch t := v.(type) {
	case map[string]any:
		for _, x := range t {
			n = countNodes(x, n)
			if n > maxSchemaNodes {
				return n
			}
		}
	case []any:
		for _, x := range t {
			n = countNodes(x, n)
			if n > maxSchemaNodes {
				return n
			}
		}
	}
	return n
}

// Validate checks the importer's options.
func (o Options) Validate() error {
	if len(o.RiskOverrides) > maxNameMapEntries || len(o.Names) > maxNameMapEntries {
		return fail("risk_overrides and names map at most %d entries each", maxNameMapEntries)
	}
	for k, r := range o.RiskOverrides {
		if k == "" || len(k) > 300 {
			return fail("a risk_overrides key names a tool or an operation (1-300 characters)")
		}
		switch r {
		case domain.RiskRead, domain.RiskWriteReversible, domain.RiskWriteIrreversible, domain.RiskExecute, domain.RiskAdmin:
		default:
			return fail("risk_overrides[%q] must be READ, WRITE_REVERSIBLE, WRITE_IRREVERSIBLE, EXECUTE or ADMIN", k)
		}
	}
	for k, n := range o.Names {
		if k == "" || len(k) > 300 {
			return fail("a names key names an operation (1-300 characters)")
		}
		if !ToolNamePattern.MatchString(n) {
			return fail("names[%q] = %q is not a tool name (lowercase letters, digits, '-' or '_', at most 63)", k, n)
		}
	}
	return nil
}

func validRisk(s string) (domain.RiskLevel, bool) {
	r := domain.RiskLevel(strings.ToUpper(strings.TrimSpace(s)))
	switch r {
	case domain.RiskRead, domain.RiskWriteReversible, domain.RiskWriteIrreversible, domain.RiskExecute, domain.RiskAdmin:
		return r, true
	}
	return "", false
}
