// Package openapicheck holds HTTP traffic to an OpenAPI 3.1 contract
// (ADR-0021). It is the Go counterpart of agenttwin_core.openapi_contract:
// integration tests pass every response a service gives — and every request
// it accepts — through a Contract, so the document cannot drift from the
// implementation unnoticed.
//
//   - the operation must exist (method and path template) and the status must
//     be documented for it (the exact code, a 2XX-style range or default);
//   - JSON bodies must match the documented schema strictly: an object that
//     declares its properties may not carry undocumented ones. The published
//     document stays open for evolution (clients ignore unknown fields); the
//     check makes a field the service sends without documenting it fail the
//     test that sees it;
//   - a schema marked `x-agenttwin-schema: <document>` (or
//     `<document>#/$defs/<definition>`) holds an embedded document, checked
//     against the canonical schema it names (packages/scenario-schema);
//   - accepted requests must match the documented parameters and body;
//   - every checked exchange is recorded, so a test can require that each
//     operation was exercised (Uncovered).
package openapicheck

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"mime"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"github.com/santhosh-tekuri/jsonschema/v6/kind"
	"golang.org/x/text/language"
	"golang.org/x/text/message"
	"gopkg.in/yaml.v3"

	scenarioschema "github.com/Ozgurisikdamar/AgentTwin/packages/scenario-schema"
)

var (
	methods = []string{"get", "put", "post", "delete", "patch", "head", "options", "trace"}
	// Keywords next to a $ref that do not constrain anything.
	annotations = map[string]bool{"description": true, "summary": true, "title": true, "example": true, "examples": true, "deprecated": true}
	// Keywords whose values are data, not schemas.
	dataKeywords = map[string]bool{"enum": true, "const": true, "default": true, "example": true, "examples": true}
	schemaMaps   = []string{"properties", "patternProperties", "$defs"}
	schemaValues = []string{
		"items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems",
		"contains", "propertyNames",
	}
	// Subschemas that add to the object they sit in: their properties count
	// as declared by it, so they are not closed at their own top level. `if`
	// and `not` are conditions and are left as written: closing one would
	// change what it matches.
	inPlaceValues   = []string{"then", "else"}
	inPlaceMaps     = []string{"dependentSchemas"}
	embeddedName    = regexp.MustCompile(`^([a-z][a-z0-9-]*\.v[0-9]+)(?:#/\$defs/([A-Za-z][A-Za-z0-9_]*))?$`)
	uuidPattern     = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	dateTimePattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$`)
	printer         = message.NewPrinter(language.English)
)

// Violation is a request or response that contradicts the contract.
type Violation struct{ Message string }

func (v *Violation) Error() string { return v.Message }

func violation(format string, args ...any) error {
	return &Violation{Message: fmt.Sprintf(format, args...)}
}

// Operation is one documented operation.
type Operation struct {
	Method   string // upper case; POST for a webhook
	Path     string // the path template, or "webhook:<name>"
	ID       string // operationId
	def      map[string]any
	params   []map[string]any
	segments []string
}

// webhook reports whether op is a call the service makes rather than serves.
func (op *Operation) webhook() bool { return strings.HasPrefix(op.Path, "webhook:") }

// Contract is a loaded OpenAPI 3.1 document.
type Contract struct {
	Name string
	doc  map[string]any
	ops  []*Operation

	mu       sync.Mutex
	seen     map[string]map[int]bool
	compiled map[string]*jsonschema.Schema
	trees    map[string]any // resource location -> the strict schema compiled there
}

// Load reads an OpenAPI 3.1 document (YAML or JSON) from fsys. The contract
// is named after the file (`control-plane.openapi.yaml` → `control-plane`).
func Load(fsys fs.FS, name string) (*Contract, error) {
	raw, err := fs.ReadFile(fsys, name)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", name, err)
	}
	var parsed any
	if err := yaml.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("parse %s: %w", name, err)
	}
	// Through JSON: the schema library works on encoding/json values.
	encoded, err := json.Marshal(stringKeys(parsed))
	if err != nil {
		return nil, fmt.Errorf("convert %s: %w", name, err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(encoded))
	if err != nil {
		return nil, fmt.Errorf("convert %s: %w", name, err)
	}
	m, ok := doc.(map[string]any)
	if !ok || !strings.HasPrefix(fmt.Sprint(m["openapi"]), "3.1") {
		return nil, fmt.Errorf("%s is not an OpenAPI 3.1 document", name)
	}
	return New(m, strings.SplitN(path.Base(name), ".", 2)[0])
}

// stringKeys turns YAML mappings with non-string keys into string-keyed maps.
func stringKeys(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, x := range t {
			out[k] = stringKeys(x)
		}
		return out
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, x := range t {
			out[fmt.Sprint(k)] = stringKeys(x)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = stringKeys(x)
		}
		return out
	default:
		return v
	}
}

// New builds a contract from a parsed document (encoding/json values).
func New(doc map[string]any, name string) (*Contract, error) {
	c := &Contract{
		Name: name, doc: doc,
		seen: map[string]map[int]bool{}, compiled: map[string]*jsonschema.Schema{}, trees: map[string]any{},
	}
	// An embedded document the checker cannot validate is a broken contract,
	// reported now rather than by the first response that carries one.
	if err := checkMarkers(doc); err != nil {
		return nil, err
	}
	paths, _ := doc["paths"].(map[string]any)
	for _, p := range sortedKeys(paths) {
		item, err := c.follow(paths[p])
		if err != nil {
			return nil, err
		}
		if err := c.addItem(p, item); err != nil {
			return nil, err
		}
	}
	hooks, _ := doc["webhooks"].(map[string]any)
	for _, h := range sortedKeys(hooks) {
		item, err := c.follow(hooks[h])
		if err != nil {
			return nil, err
		}
		if err := c.addItem("webhook:"+h, item); err != nil {
			return nil, err
		}
	}
	return c, nil
}

// checkMarkers resolves every x-agenttwin-schema value of a document.
func checkMarkers(node any) error {
	switch t := node.(type) {
	case map[string]any:
		for _, k := range sortedKeys(t) {
			if k == embeddedKeyword {
				if _, err := Embedded(fmt.Sprint(t[k])); err != nil {
					return err
				}
				continue
			}
			if k == "enum" || k == "const" || k == "example" || k == "examples" {
				continue // data, not schemas ("default" is also a response key)
			}
			if err := checkMarkers(t[k]); err != nil {
				return err
			}
		}
	case []any:
		for _, x := range t {
			if err := checkMarkers(x); err != nil {
				return err
			}
		}
	}
	return nil
}

func (c *Contract) addItem(p string, raw any) error {
	item, _ := raw.(map[string]any)
	var shared []map[string]any
	for _, rp := range asList(item["parameters"]) {
		f, err := c.follow(rp)
		if err != nil {
			return err
		}
		shared = append(shared, asMap(f))
	}
	for _, method := range methods {
		def, ok := item[method].(map[string]any)
		if !ok {
			continue
		}
		op := &Operation{Method: strings.ToUpper(method), Path: p, def: def}
		op.ID, _ = def["operationId"].(string)
		if op.ID == "" {
			return fmt.Errorf("%s %s has no operationId", op.Method, p)
		}
		byName := map[string]map[string]any{}
		var order []string
		for _, param := range shared {
			k := fmt.Sprint(param["in"]) + ":" + strings.ToLower(fmt.Sprint(param["name"]))
			byName[k] = param
			order = append(order, k)
		}
		for _, rp := range asList(def["parameters"]) {
			f, err := c.follow(rp)
			if err != nil {
				return err
			}
			param := asMap(f)
			k := fmt.Sprint(param["in"]) + ":" + strings.ToLower(fmt.Sprint(param["name"]))
			if _, dup := byName[k]; !dup {
				order = append(order, k)
			}
			byName[k] = param // an operation's parameter overrides the path's
		}
		for _, k := range order {
			op.params = append(op.params, byName[k])
		}
		op.segments = segments(p)
		c.ops = append(c.ops, op)
	}
	return nil
}

// Operations are the documented operations, in document order of paths.
func (c *Contract) Operations() []*Operation { return append([]*Operation(nil), c.ops...) }

// Routes are the documented path operations as "METHOD /path/{param}"
// (webhooks excluded), sorted.
func (c *Contract) Routes() []string {
	var out []string
	for _, op := range c.ops {
		if !op.webhook() {
			out = append(out, op.Method+" "+op.Path)
		}
	}
	sort.Strings(out)
	return out
}

// segments of a path, exactly: "/a/" is not "/a".
func segments(p string) []string {
	if strings.HasPrefix(p, "/") {
		return strings.Split(p, "/")[1:]
	}
	return []string{p}
}

// Find returns the operation for a concrete path and its path parameters.
// Literal segments win over templated ones (`/projects/mine` is not a project id).
func (c *Contract) Find(method, p string) (*Operation, map[string]string, bool) {
	got := segments(p)
	var best *Operation
	var bestValues map[string]string
	bestScore := -1
	for _, op := range c.ops {
		if op.Method != strings.ToUpper(method) || op.webhook() || len(op.segments) != len(got) {
			continue
		}
		values := map[string]string{}
		score := 0
		match := true
		for i, want := range op.segments {
			if strings.HasPrefix(want, "{") && strings.HasSuffix(want, "}") {
				if got[i] == "" {
					match = false
					break
				}
				v, err := url.PathUnescape(got[i])
				if err != nil {
					v = got[i]
				}
				values[want[1:len(want)-1]] = v
				continue
			}
			if want != got[i] {
				match = false
				break
			}
			score++
		}
		if match && score > bestScore {
			best, bestValues, bestScore = op, values, score
		}
	}
	return best, bestValues, best != nil
}

func (c *Contract) operation(method, p string) (*Operation, map[string]string, error) {
	op, values, ok := c.Find(method, p)
	if !ok {
		return nil, nil, violation("%s: %s %s is not a documented operation", c.Name, strings.ToUpper(method), p)
	}
	return op, values, nil
}

func (c *Contract) webhook(name string) (*Operation, error) {
	for _, op := range c.ops {
		if op.Path == "webhook:"+name {
			return op, nil
		}
	}
	return nil, violation("%s: no webhook %q is documented", c.Name, name)
}

// ---------------------------------------------------------------- references

func (c *Contract) pointer(ref string) (any, error) {
	if !strings.HasPrefix(ref, "#/") {
		return nil, fmt.Errorf("only local references are supported, not %q", ref)
	}
	var node any = c.doc
	for _, part := range strings.Split(ref[2:], "/") {
		part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
		m, ok := node.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("unresolvable $ref %q", ref)
		}
		if node, ok = m[part]; !ok {
			return nil, fmt.Errorf("unresolvable $ref %q", ref)
		}
	}
	return node, nil
}

// follow returns the object a (chain of) $ref points to.
func (c *Contract) follow(node any) (any, error) {
	seen := map[string]bool{}
	for {
		m, ok := node.(map[string]any)
		if !ok {
			return node, nil
		}
		ref, ok := m["$ref"].(string)
		if !ok {
			return node, nil
		}
		if seen[ref] {
			return nil, fmt.Errorf("cyclic $ref %q", ref)
		}
		seen[ref] = true
		var err error
		if node, err = c.pointer(ref); err != nil {
			return nil, err
		}
	}
}

// inline returns node with every schema reference replaced by its target.
func (c *Contract) inline(node any, stack []string) (any, error) {
	switch t := node.(type) {
	case []any:
		out := make([]any, len(t))
		for i, item := range t {
			v, err := c.inline(item, stack)
			if err != nil {
				return nil, err
			}
			out[i] = v
		}
		return out, nil
	case map[string]any:
		if ref, ok := t["$ref"].(string); ok {
			for _, s := range stack {
				if s == ref {
					return nil, fmt.Errorf("cyclic $ref %q", ref)
				}
			}
			resolved, err := c.pointer(ref)
			if err != nil {
				return nil, err
			}
			target, err := c.inline(resolved, append(append([]string(nil), stack...), ref))
			if err != nil {
				return nil, err
			}
			siblings := map[string]any{}
			for k, v := range t {
				if k == "$ref" || annotations[k] {
					continue
				}
				iv, err := c.inline(v, stack)
				if err != nil {
					return nil, err
				}
				siblings[k] = iv
			}
			if len(siblings) == 0 {
				return target, nil
			}
			siblings["allOf"] = []any{target}
			return siblings, nil
		}
		out := make(map[string]any, len(t))
		for k, v := range t {
			if dataKeywords[k] {
				out[k] = v
				continue
			}
			iv, err := c.inline(v, stack)
			if err != nil {
				return nil, err
			}
			out[k] = iv
		}
		return out, nil
	default:
		return node, nil
	}
}

// Strictify returns a copy of schema in which objects that declare
// properties reject undeclared ones (unevaluatedProperties: false), unless the
// schema says otherwise. The members of an allOf — like then, else and
// dependentSchemas, which also add to the object they sit in — are exempt at
// their top level: the object as a whole is closed instead, so their
// properties count as declared. if and not are conditions and are left as
// written.
func Strictify(schema any) any { return strictify(schema, false) }

func strictify(schema any, member bool) any {
	m, ok := schema.(map[string]any)
	if !ok {
		return schema
	}
	out := make(map[string]any, len(m)+1)
	for k, v := range m {
		out[k] = v
	}
	for _, key := range schemaMaps {
		if sub, ok := out[key].(map[string]any); ok {
			strict := make(map[string]any, len(sub))
			for name, s := range sub {
				strict[name] = strictify(s, false)
			}
			out[key] = strict
		}
	}
	for _, key := range schemaValues {
		if sub, ok := out[key].(map[string]any); ok {
			out[key] = strictify(sub, false)
		}
	}
	for _, key := range inPlaceValues {
		if sub, ok := out[key].(map[string]any); ok {
			out[key] = strictify(sub, true)
		}
	}
	for _, key := range inPlaceMaps {
		if sub, ok := out[key].(map[string]any); ok {
			strict := make(map[string]any, len(sub))
			for name, s := range sub {
				strict[name] = strictify(s, true)
			}
			out[key] = strict
		}
	}
	for _, key := range []string{"prefixItems", "anyOf", "oneOf"} {
		if list, ok := out[key].([]any); ok {
			strict := make([]any, len(list))
			for i, s := range list {
				strict[i] = strictify(s, false)
			}
			out[key] = strict
		}
	}
	if list, ok := out["allOf"].([]any); ok {
		strict := make([]any, len(list))
		for i, s := range list {
			strict[i] = strictify(s, true)
		}
		out["allOf"] = strict
	}
	_, hasProps := out["properties"]
	_, hasAll := out["allOf"]
	_, hasAdditional := out["additionalProperties"]
	_, hasUnevaluated := out["unevaluatedProperties"]
	if (hasProps || hasAll) && !member && !hasAdditional && !hasUnevaluated {
		out["unevaluatedProperties"] = false
	}
	return out
}

// ---------------------------------------------------------------- embedded documents

const embeddedKeyword = "x-agenttwin-schema"

var (
	embeddedMu    sync.Mutex
	embeddedCache = map[string]*jsonschema.Schema{}
)

// Embedded returns the canonical schema an x-agenttwin-schema value names: a
// document schema (`scenario.v1`) or one of its definitions
// (`scenario.v1#/$defs/fault`), from packages/scenario-schema. It is compiled
// as the services compile it: standard formats, no strictness added.
func Embedded(name string) (*jsonschema.Schema, error) {
	m := embeddedName.FindStringSubmatch(name)
	if m == nil {
		return nil, fmt.Errorf("invalid %s %q", embeddedKeyword, name)
	}
	embeddedMu.Lock()
	defer embeddedMu.Unlock()
	if s, ok := embeddedCache[name]; ok {
		return s, nil
	}
	file := "schemas/" + m[1] + ".schema.json"
	raw, err := fs.ReadFile(scenarioschema.Schemas, file)
	if err != nil {
		return nil, fmt.Errorf("%s %q: there is no canonical schema %s", embeddedKeyword, name, m[1])
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", file, err)
	}
	id, _ := asMap(doc)["$id"].(string)
	if id == "" {
		id = "mem:///" + file
	}
	loc := id
	if m[2] != "" {
		if _, ok := asMap(asMap(doc)["$defs"])[m[2]]; !ok {
			return nil, fmt.Errorf("%s has no definition %s", m[1], m[2])
		}
		loc = id + "#/$defs/" + m[2]
	}
	comp := jsonschema.NewCompiler()
	comp.AssertFormat()
	if err := comp.AddResource(id, doc); err != nil {
		return nil, fmt.Errorf("add %s: %w", file, err)
	}
	s, err := comp.Compile(loc)
	if err != nil {
		return nil, fmt.Errorf("compile %s: %w", name, err)
	}
	embeddedCache[name] = s
	return s, nil
}

// embeddedError is one problem of an embedded document, reported where the
// document sits: "is not a valid scenario.v1 fault: behavior/type: …".
type embeddedError struct{ what, where, message string }

func (e *embeddedError) KeywordPath() []string { return []string{embeddedKeyword} }

func (e *embeddedError) LocalizedString(*message.Printer) string {
	return fmt.Sprintf("is not a valid %s: %s: %s", e.what, e.where, e.message)
}

// embedded validates the value of a marked schema against its canonical
// schema, independently of the contract's own (strict) validation.
type embedded struct {
	what   string
	schema *jsonschema.Schema
}

func (e *embedded) Validate(ctx *jsonschema.ValidatorContext, v any) {
	err := e.schema.Validate(v)
	if err == nil {
		return
	}
	var ve *jsonschema.ValidationError
	if !errors.As(err, &ve) {
		ctx.AddError(&embeddedError{e.what, "(root)", err.Error()})
		return
	}
	for _, l := range leaves(ve) {
		where := strings.Join(l.InstanceLocation, "/")
		if where == "" {
			where = "(root)"
		}
		msg := l.ErrorKind.LocalizedString(printer)
		if _, ok := l.ErrorKind.(*kind.FalseSchema); ok {
			msg = "not allowed here"
		}
		ctx.AddError(&embeddedError{e.what, where, msg})
	}
}

// vocabulary makes x-agenttwin-schema a keyword of the contract's schemas.
var vocabulary = &jsonschema.Vocabulary{
	URL: "https://agenttwin.dev/openapicheck/vocab/embedded",
	Compile: func(_ *jsonschema.CompilerContext, obj map[string]any) (jsonschema.SchemaExt, error) {
		raw, ok := obj[embeddedKeyword]
		if !ok {
			return nil, nil
		}
		name := fmt.Sprint(raw)
		s, err := Embedded(name)
		if err != nil {
			return nil, err
		}
		doc, definition, _ := strings.Cut(name, "#/$defs/")
		what := doc + " document"
		if definition != "" {
			what = doc + " " + definition
		}
		return &embedded{what: what, schema: s}, nil
	},
}

// ---------------------------------------------------------------- schemas

// newCompiler compiles the schemas of values the service reads (reads) or
// writes: UUIDs are case-insensitive on input and lower-case on output
// (RFC 9562), so a service writes the canonical form and accepts either.
func newCompiler(reads bool) *jsonschema.Compiler {
	comp := jsonschema.NewCompiler()
	comp.DefaultDraft(jsonschema.Draft2020)
	comp.AssertFormat()
	comp.RegisterVocabulary(vocabulary)
	comp.AssertVocabs()
	comp.RegisterFormat(&jsonschema.Format{Name: "uuid", Validate: func(v any) error {
		s, ok := v.(string)
		switch {
		case !ok:
			return nil
		case reads && !uuidPattern.MatchString(strings.ToLower(s)):
			return errors.New("not a hyphenated UUID")
		case !reads && !uuidPattern.MatchString(s):
			return errors.New("not a canonical (lower-case) UUID")
		}
		return nil
	}})
	comp.RegisterFormat(&jsonschema.Format{Name: "date-time", Validate: func(v any) error {
		s, ok := v.(string)
		if !ok {
			return nil
		}
		if !dateTimePattern.MatchString(s) {
			return errors.New("not an RFC 3339 date-time with a T and a zone")
		}
		if _, err := time.Parse(time.RFC3339Nano, s); err != nil {
			return errors.New("not a valid date-time")
		}
		return nil
	}})
	comp.RegisterFormat(&jsonschema.Format{Name: "uri", Validate: func(v any) error {
		if s, ok := v.(string); ok {
			u, err := url.Parse(s)
			if err != nil || u.Scheme == "" || u.Host == "" {
				return errors.New("not an absolute URI")
			}
		}
		return nil
	}})
	return comp
}

func (c *Contract) schema(key string, reads bool, schema any) (*jsonschema.Schema, error) {
	if reads {
		key += "|reads"
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if s, ok := c.compiled[key]; ok {
		return s, nil
	}
	inlined, err := c.inline(schema, nil)
	if err != nil {
		return nil, err
	}
	strict := strictify(inlined, false)
	comp := newCompiler(reads)
	loc := fmt.Sprintf("mem:///%s/%d.json", url.PathEscape(c.Name), len(c.compiled))
	if err := comp.AddResource(loc, strict); err != nil {
		return nil, err
	}
	s, err := comp.Compile(loc)
	if err != nil {
		return nil, err
	}
	c.compiled[key] = s
	c.trees[loc] = strict
	return s, nil
}

// leaves are the errors of a validation error that have no causes.
func leaves(ve *jsonschema.ValidationError) []*jsonschema.ValidationError {
	if len(ve.Causes) == 0 {
		return []*jsonschema.ValidationError{ve}
	}
	var out []*jsonschema.ValidationError
	for _, cause := range ve.Causes {
		out = append(out, leaves(cause)...)
	}
	return out
}

// declares reports whether a field left unevaluated (by the
// unevaluatedProperties keyword at schemaURL) is documented for its object
// all the same: declared by some subschema that applies to the same value — a
// member of an allOf that failed (its cause is reported on its own) or a
// variant of an anyOf/oneOf the value does not match.
func (c *Contract) declares(schemaURL, name string) bool {
	loc, fragment, _ := strings.Cut(schemaURL, "#")
	c.mu.Lock()
	tree, ok := c.trees[loc]
	c.mu.Unlock()
	if !ok || !strings.HasPrefix(fragment, "/") {
		return false
	}
	tokens := strings.Split(fragment[1:], "/")
	tokens = tokens[:len(tokens)-1] // the schema that owns the keyword
	// Up to the schema of the value itself: in-place applicators apply to the
	// same value, every other keyword leads to another one.
	for len(tokens) > 0 {
		n := len(tokens)
		switch {
		case n >= 2 && (tokens[n-2] == "allOf" || tokens[n-2] == "anyOf" || tokens[n-2] == "oneOf" || tokens[n-2] == "dependentSchemas"):
			tokens = tokens[:n-2]
			continue
		case tokens[n-1] == "if" || tokens[n-1] == "then" || tokens[n-1] == "else":
			tokens = tokens[:n-1]
			continue
		}
		break
	}
	node := tree
	for _, tok := range tokens {
		tok = strings.ReplaceAll(strings.ReplaceAll(tok, "~1", "/"), "~0", "~")
		switch t := node.(type) {
		case map[string]any:
			node = t[tok]
		case []any:
			i, err := strconv.Atoi(tok)
			if err != nil || i < 0 || i >= len(t) {
				return false
			}
			node = t[i]
		default:
			return false
		}
	}
	return declared(node, name)
}

func declared(node any, name string) bool {
	m, ok := node.(map[string]any)
	if !ok {
		return false
	}
	if _, ok := asMap(m["properties"])[name]; ok {
		return true
	}
	for pattern := range asMap(m["patternProperties"]) {
		if ok, err := regexp.MatchString(pattern, name); err == nil && ok {
			return true
		}
	}
	for _, key := range []string{"allOf", "anyOf", "oneOf"} {
		for _, sub := range asList(m[key]) {
			if declared(sub, name) {
				return true
			}
		}
	}
	for _, key := range []string{"if", "then", "else"} {
		if declared(m[key], name) {
			return true
		}
	}
	for _, sub := range asMap(m["dependentSchemas"]) {
		if declared(sub, name) {
			return true
		}
	}
	return false
}

// problems are the leaf errors of a validation error, as "  at <pointer>:
// <message>" lines. An undocumented field is reported as such; a documented
// field is also left unevaluated when the subschema declaring it fails, and
// that echo is dropped so that only the cause is reported — unless it is all
// there is (a field of an anyOf/oneOf variant the value does not match).
func (c *Contract) problems(err error) []string {
	var ve *jsonschema.ValidationError
	if !errors.As(err, &ve) {
		return []string{"  " + err.Error()}
	}
	type problem struct{ path, msg string }
	var kept, echoes []problem
	for _, l := range leaves(ve) {
		p := problem{"/" + strings.Join(l.InstanceLocation, "/"), l.ErrorKind.LocalizedString(printer)}
		_, isFalse := l.ErrorKind.(*kind.FalseSchema)
		if isFalse && strings.HasSuffix(l.SchemaURL, "/unevaluatedProperties") && len(l.InstanceLocation) > 0 {
			if c.declares(l.SchemaURL, l.InstanceLocation[len(l.InstanceLocation)-1]) {
				p.msg = "field of a variant this value does not match"
				echoes = append(echoes, p)
				continue
			}
			p.msg = "undocumented field (not in the contract)"
		}
		kept = append(kept, p)
	}
	if len(kept) == 0 {
		// The most specific ones: a field whose own value is reported is not
		// reported again as the field of its object.
		for _, e := range echoes {
			deeper := false
			for _, o := range echoes {
				if strings.HasPrefix(o.path, e.path+"/") {
					deeper = true
					break
				}
			}
			if !deeper {
				kept = append(kept, e)
			}
		}
	}
	var out []string
	seen := map[string]bool{}
	for _, p := range kept {
		line := "  at " + p.path + ": " + p.msg
		if !seen[line] {
			seen[line] = true
			out = append(out, line)
		}
	}
	sort.Strings(out)
	if len(out) > 10 {
		out = out[:10]
	}
	return out
}

// validate checks instance against schema; reads says the service reads the
// value (a request of an operation, the answer to a webhook call) rather than
// writes it.
func (c *Contract) validate(key string, reads bool, schema any, instance any) []string {
	s, err := c.schema(key, reads, schema)
	if err != nil {
		return []string{"  the contract's schema does not compile: " + err.Error()}
	}
	if err := s.Validate(instance); err != nil {
		return c.problems(err)
	}
	return nil
}

// CheckSchema checks a value against a component schema (#/components/schemas/<name>).
func (c *Contract) CheckSchema(name string, instance any) error {
	ref := "#/components/schemas/" + strings.ReplaceAll(strings.ReplaceAll(name, "~", "~0"), "/", "~1")
	if p := c.validate("schema:"+name, false, map[string]any{"$ref": ref}, normalize(instance)); p != nil {
		return violation("%s: not a valid %s:\n%s", c.Name, name, strings.Join(p, "\n"))
	}
	return nil
}

// normalize converts a Go value to the encoding/json form the validator expects.
func normalize(v any) any {
	raw, err := json.Marshal(v)
	if err != nil {
		return v
	}
	out, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return v
	}
	return out
}

// ---------------------------------------------------------------- checks

func (c *Contract) responseFor(op *Operation, status int) (string, map[string]any, bool) {
	responses, _ := op.def["responses"].(map[string]any)
	s := strconv.Itoa(status)
	for _, key := range []string{s, s[:1] + "XX", "default"} {
		if r, ok := responses[key]; ok {
			f, err := c.follow(r)
			if err != nil {
				return "", nil, false
			}
			return key, asMap(f), true
		}
	}
	return "", nil, false
}

func (c *Contract) checkBody(where string, content any, contentType string, body []byte, key string, reads bool) error {
	media := asMap(content)
	if len(media) == 0 {
		if len(bytes.TrimSpace(body)) > 0 {
			return violation("%s: no body is documented, but one was sent", where)
		}
		return nil
	}
	mt, _, _ := mime.ParseMediaType(contentType)
	mt = strings.ToLower(mt)
	entry, ok := media[mt]
	if !ok {
		name := mt
		if name == "" {
			name = "(none)"
		}
		return violation("%s: content type %s is not documented (%s)", where, name, strings.Join(sortedKeys(media), ", "))
	}
	schema, ok := asMap(entry)["schema"]
	if m, isMap := schema.(map[string]any); !ok || schema == nil || schema == true || (isMap && len(m) == 0) {
		return nil // any content
	}
	if mt != "application/json" && !strings.HasSuffix(mt, "+json") {
		return nil
	}
	instance, err := jsonschema.UnmarshalJSON(bytes.NewReader(body))
	if err != nil {
		return violation("%s: the body is not JSON (%v)", where, err)
	}
	if p := c.validate(key, reads, schema, instance); p != nil {
		return violation("%s does not match the contract:\n%s", where, strings.Join(p, "\n"))
	}
	return nil
}

func (c *Contract) checkResponseOf(op *Operation, status int, contentType string, body []byte) error {
	key, response, ok := c.responseFor(op, status)
	if !ok {
		return violation("%s: %s answered %d, which is not documented", c.Name, op.ID, status)
	}
	return c.checkBody(fmt.Sprintf("%s: %s %d response", c.Name, op.ID, status), response["content"], contentType, body, op.ID+"|response|"+key, op.webhook())
}

// coerce reads a parameter value as the type its schema declares.
func coerce(raw string, schema map[string]any) any {
	kinds := map[string]bool{}
	switch t := schema["type"].(type) {
	case string:
		kinds[t] = true
	case []any:
		for _, x := range t {
			kinds[fmt.Sprint(x)] = true
		}
	}
	if kinds["integer"] {
		if _, err := strconv.ParseInt(raw, 10, 64); err == nil {
			return json.Number(raw)
		}
		return raw
	}
	if kinds["number"] {
		if _, err := strconv.ParseFloat(raw, 64); err == nil {
			return json.Number(raw)
		}
		return raw
	}
	if kinds["boolean"] && (raw == "true" || raw == "false") {
		return raw == "true"
	}
	return raw
}

func (c *Contract) checkParameters(op *Operation, location string, given map[string][]string) error {
	documented := map[string]map[string]any{}
	for _, p := range op.params {
		if p["in"] == location {
			documented[strings.ToLower(fmt.Sprint(p["name"]))] = p
		}
	}
	for _, name := range sortedKeys(given) {
		param, ok := documented[strings.ToLower(name)]
		if !ok {
			return violation("%s: %s was called with the undocumented %s parameter %q", c.Name, op.ID, location, name)
		}
		schema := param["schema"]
		if schema == nil {
			schema = map[string]any{}
		}
		inlined, err := c.inline(schema, nil)
		if err != nil {
			return err
		}
		for _, raw := range given[name] {
			key := op.ID + "|" + location + "|" + strings.ToLower(name)
			if p := c.validate(key, true, schema, coerce(raw, asMap(inlined))); p != nil {
				return violation("%s: %s %s parameter %q=%q:\n%s", c.Name, op.ID, location, name, raw, strings.Join(p, "\n"))
			}
		}
	}
	for name, param := range documented {
		if required, _ := param["required"].(bool); !required {
			continue
		}
		present := false
		for g := range given {
			if strings.ToLower(g) == name {
				present = true
			}
		}
		if !present {
			return violation("%s: %s requires the %s parameter %q", c.Name, op.ID, location, param["name"])
		}
	}
	return nil
}

func (c *Contract) checkRequestOf(op *Operation, pathValues map[string]string, query url.Values, header http.Header, contentType string, body []byte) error {
	pv := map[string][]string{}
	for k, v := range pathValues {
		pv[k] = []string{v}
	}
	if err := c.checkParameters(op, "path", pv); err != nil {
		return err
	}
	if err := c.checkParameters(op, "query", query); err != nil {
		return err
	}
	documented := map[string]bool{}
	for _, p := range op.params {
		if p["in"] == "header" {
			documented[strings.ToLower(fmt.Sprint(p["name"]))] = true
		}
	}
	present := map[string][]string{}
	for k, v := range header {
		if documented[strings.ToLower(k)] {
			present[strings.ToLower(k)] = v
		}
	}
	if err := c.checkParameters(op, "header", present); err != nil {
		return err
	}
	rb, err := c.follow(op.def["requestBody"])
	if err != nil {
		return err
	}
	requestBody := asMap(rb)
	if len(requestBody) == 0 {
		if len(bytes.TrimSpace(body)) > 0 {
			return violation("%s: %s documents no request body", c.Name, op.ID)
		}
		return nil
	}
	if len(bytes.TrimSpace(body)) == 0 {
		if required, _ := requestBody["required"].(bool); required {
			return violation("%s: %s requires a request body", c.Name, op.ID)
		}
		return nil
	}
	return c.checkBody(fmt.Sprintf("%s: %s request", c.Name, op.ID), requestBody["content"], contentType, body, op.ID+"|request", !op.webhook())
}

func (c *Contract) record(op *Operation, status int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.seen[op.ID] == nil {
		c.seen[op.ID] = map[int]bool{}
	}
	c.seen[op.ID][status] = true
}

// CheckResponse checks one response and records it for Uncovered.
func (c *Contract) CheckResponse(method, p string, status int, contentType string, body []byte) error {
	op, _, err := c.operation(method, p)
	if err != nil {
		return err
	}
	if err := c.checkResponseOf(op, status, contentType, body); err != nil {
		return err
	}
	c.record(op, status)
	return nil
}

// CheckRequest checks a request the server accepted: its parameters and body
// are what the contract documents.
func (c *Contract) CheckRequest(method, p string, query url.Values, header http.Header, contentType string, body []byte) error {
	op, values, err := c.operation(method, p)
	if err != nil {
		return err
	}
	return c.checkRequestOf(op, values, query, header, contentType, body)
}

// CheckExchange checks a response and — when the server accepted the request
// (2xx) — the request as well; a passing exchange is recorded for Uncovered.
// Bodies are the bytes on the wire: a content coding (gzip) is undone first.
func (c *Contract) CheckExchange(r *http.Request, requestBody []byte, status int, header http.Header, body []byte) error {
	op, values, err := c.operation(r.Method, r.URL.Path)
	if err != nil {
		return err
	}
	if body, err = decoded(header.Get("Content-Encoding"), body); err != nil {
		return violation("%s: %s %d response: %v", c.Name, op.ID, status, err)
	}
	if err := c.checkResponseOf(op, status, header.Get("Content-Type"), body); err != nil {
		return err
	}
	if status >= 200 && status < 300 {
		if requestBody, err = decoded(r.Header.Get("Content-Encoding"), requestBody); err != nil {
			return violation("%s: %s request: %v", c.Name, op.ID, err)
		}
		if err := c.checkRequestOf(op, values, r.URL.Query(), r.Header, r.Header.Get("Content-Type"), requestBody); err != nil {
			return err
		}
	}
	c.record(op, status)
	return nil
}

// maxDecoded bounds a decoded body.
const maxDecoded = 64 << 20

// decoded undoes a content coding: a schema describes the representation,
// not its coding on the wire.
func decoded(coding string, body []byte) ([]byte, error) {
	switch strings.ToLower(strings.TrimSpace(coding)) {
	case "", "identity":
		return body, nil
	case "gzip", "x-gzip":
		zr, err := gzip.NewReader(bytes.NewReader(body))
		if err != nil {
			return nil, fmt.Errorf("the body is not valid gzip (%w)", err)
		}
		defer func() { _ = zr.Close() }()
		out, err := io.ReadAll(io.LimitReader(zr, maxDecoded+1))
		switch {
		case err != nil:
			return nil, fmt.Errorf("the body is not valid gzip (%w)", err)
		case len(out) > maxDecoded:
			return nil, fmt.Errorf("the decoded body is larger than %d bytes", maxDecoded)
		}
		return out, nil
	}
	return nil, fmt.Errorf("content coding %q cannot be checked", coding)
}

// Checking wraps a server's handler so that every exchange on a documented
// operation is checked with CheckExchange; a violation goes to report, which
// runs on the serving goroutine. The request body is recorded as the handler
// reads it, so a streaming handler keeps streaming; after an accepted (2xx)
// request the rest is read as well, to check the body whole. Requests on
// undocumented paths pass through unchecked.
func (c *Contract) Checking(next http.Handler, report func(error)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, _, ok := c.Find(r.Method, r.URL.Path); !ok {
			next.ServeHTTP(w, r)
			return
		}
		var requestBody bytes.Buffer
		body := r.Body
		r.Body = struct {
			io.Reader
			io.Closer
		}{io.TeeReader(body, &requestBody), body}
		rec := &recorder{ResponseWriter: w}
		next.ServeHTTP(rec, r)
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		if status >= 200 && status < 300 {
			_, _ = io.Copy(&requestBody, io.LimitReader(body, maxDecoded))
		}
		if err := c.CheckExchange(r, requestBody.Bytes(), status, w.Header(), rec.body.Bytes()); err != nil {
			report(err)
		}
	})
}

// recorder keeps what a handler answers while passing it on.
type recorder struct {
	http.ResponseWriter
	status int
	body   bytes.Buffer
}

func (r *recorder) WriteHeader(code int) {
	if r.status == 0 && code >= 200 {
		r.status = code
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *recorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	r.body.Write(b)
	return r.ResponseWriter.Write(b)
}

// Flush passes a flush on, for handlers that stream.
func (r *recorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Unwrap gives http.ResponseController the underlying writer.
func (r *recorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// CheckWebhook checks a call made to a documented webhook: always the
// request, the response when there is one (status 0: no response).
func (c *Contract) CheckWebhook(name string, contentType string, requestBody []byte, status int, responseType string, responseBody []byte) error {
	op, err := c.webhook(name)
	if err != nil {
		return err
	}
	if err := c.checkRequestOf(op, nil, nil, nil, contentType, requestBody); err != nil {
		return err
	}
	if status == 0 {
		return nil
	}
	if err := c.checkResponseOf(op, status, responseType, responseBody); err != nil {
		return err
	}
	c.record(op, status)
	return nil
}

// Uncovered lists the operations without a successful (2xx) exchange that
// passed its check.
func (c *Contract) Uncovered() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for _, op := range c.ops {
		ok := false
		for status := range c.seen[op.ID] {
			if status >= 200 && status < 300 {
				ok = true
			}
		}
		if !ok {
			out = append(out, op.ID)
		}
	}
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------- helpers

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
