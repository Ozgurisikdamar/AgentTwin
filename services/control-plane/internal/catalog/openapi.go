package catalog

import (
	"net/url"
	"sort"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// methods in the order operations are read.
var methods = []string{"get", "put", "post", "delete", "options", "head", "patch", "trace"}

// safeMethods are read-like by HTTP's definition; a document can still say
// otherwise (spec §20.2: the verb alone does not define risk).
var safeMethods = map[string]bool{"GET": true, "HEAD": true, "OPTIONS": true, "TRACE": true}

// FromOpenAPI reads an OpenAPI 3.0 or 3.1 document: every operation becomes
// an entry whose input schema holds its parameters (by name) and its JSON
// request body (as "body").
func FromOpenAPI(doc map[string]any, opts Options) (Catalog, error) {
	if err := opts.Validate(); err != nil {
		return Catalog{}, err
	}
	version := str(doc["openapi"])
	switch {
	case str(doc["swagger"]) != "":
		return Catalog{}, fail("Swagger %s is not supported; convert the document to OpenAPI 3", str(doc["swagger"]))
	case !strings.HasPrefix(version, "3."):
		return Catalog{}, fail("not an OpenAPI 3 document (openapi: %q)", version)
	}
	paths := asMap(doc["paths"])
	if paths == nil {
		return Catalog{}, fail("the document has no paths")
	}
	b := newBuilder(SourceOpenAPI, opts)
	info := asMap(doc["info"])
	b.c.Title = cleanText(str(info["title"]), 200)
	b.c.APIVersion = cleanText(str(info["version"]), 100)
	b.c.SpecVersion = cleanText(version, 20)
	for _, s := range asList(doc["servers"]) {
		if u := serverURL(str(asMap(s)["url"])); u != "" && len(b.c.Servers) < maxServers {
			b.c.Servers = append(b.c.Servers, u)
		}
	}
	r := &resolver{doc: doc, b: b}
	count := 0
	for _, p := range sortedKeys(paths) {
		item := asMap(r.resolveRef(p, paths[p]))
		if item == nil {
			continue
		}
		for _, m := range methods {
			op := asMap(item[m])
			if op == nil {
				continue
			}
			count++
			if count > MaxEntries*2 {
				return Catalog{}, fail("the document has more than %d operations", MaxEntries*2)
			}
			b.operation(r, strings.ToUpper(m), p, item, op)
		}
	}
	return b.finish(), nil
}

// serverURL keeps the scheme, host and path of a server URL: user
// information and query strings can carry credentials.
func serverURL(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || raw == "" {
		return ""
	}
	u.User, u.RawQuery, u.Fragment = nil, "", ""
	return cleanText(u.String(), 300)
}

func (b *builder) operation(r *resolver, method, path string, item, op map[string]any) {
	opID := cleanText(str(op["operationId"]), 200)
	label := method + " " + path
	operation := opID
	if operation == "" {
		operation = label
	}
	name, ok := b.name(operation, []string{opID, label}, str(op["x-agenttwin-tool"]),
		nonEmpty(opID, strings.ToLower(method)+"_"+path))
	if !ok {
		return
	}
	e := Entry{Name: name, Operation: operation, Method: method, Path: cleanText(path, 500),
		Title: cleanText(str(op["summary"]), 200), Deprecated: op["deprecated"] == true}
	e.Description = cleanText(nonEmpty(str(op["description"]), str(op["summary"])), maxDescription)
	for _, t := range asList(op["tags"]) {
		if s := cleanText(str(t), 100); s != "" && len(e.Tags) < maxTags {
			e.Tags = append(e.Tags, s)
		}
	}
	e.InputSchema = b.boundSchema(operation, b.inputSchema(r, operation, item, op))

	switch declared := str(op["x-agenttwin-risk"]); {
	case declared != "":
		if risk, ok := validRisk(declared); ok {
			e.Risk, e.RiskSource = risk, RiskDeclared
		} else {
			b.warn("%s: x-agenttwin-risk %q is not a risk level; ignored", operation, declared)
		}
	}
	if risk, ok := b.override(name, opID, label); ok {
		e.Risk, e.RiskSource = risk, RiskOverride
	}
	if e.Risk == "" {
		e.RiskSource = RiskInferred
		e.Risk = domain.DefaultRisk
		if safeMethods[method] {
			e.Risk = domain.RiskRead
		}
	}
	if e.Risk == domain.RiskRead && !safeMethods[method] {
		b.warn("%s: a %s operation is recorded as READ (%s); check that it changes nothing", operation, method, e.RiskSource)
	}
	b.add(e)
}

// inputSchema is the object a caller passes: the operation's parameters by
// name, and its request body as "body".
func (b *builder) inputSchema(r *resolver, operation string, item, op map[string]any) map[string]any {
	type param struct {
		name, in string
		p        map[string]any
	}
	var params []param
	seen := map[string]int{}
	for _, list := range [][]any{asList(item["parameters"]), asList(op["parameters"])} {
		for _, raw := range list {
			p := asMap(r.resolveRef(operation, raw))
			name, in := str(p["name"]), str(p["in"])
			if name == "" || in == "" {
				continue
			}
			// The operation's own parameter replaces the path item's.
			if i, ok := seen[in+"\x00"+name]; ok {
				params[i] = param{name, in, p}
				continue
			}
			seen[in+"\x00"+name] = len(params)
			params = append(params, param{name, in, p})
		}
	}
	props := map[string]any{}
	var required []string
	for _, pr := range params {
		// Credentials are the runtime's to add, not the model's.
		if pr.in == "cookie" || (pr.in == "header" && (strings.EqualFold(pr.name, "authorization") || strings.EqualFold(pr.name, "cookie"))) {
			continue
		}
		if _, taken := props[pr.name]; taken {
			b.warn("%s: two parameters are named %s; the %s one is left out", operation, pr.name, pr.in)
			continue
		}
		schema := asMap(r.schema(operation, pr.p["schema"]))
		if schema == nil {
			// OpenAPI allows a parameter described by content instead.
			schema = asMap(r.schema(operation, mediaSchema(asMap(pr.p["content"]))))
		}
		if schema == nil {
			schema = map[string]any{}
		}
		if d := cleanText(str(pr.p["description"]), maxDescription); d != "" && schema["description"] == nil {
			schema["description"] = d
		}
		props[pr.name] = schema
		if pr.in == "path" || pr.p["required"] == true {
			required = append(required, pr.name)
		}
	}
	if body := asMap(r.resolveRef(operation, op["requestBody"])); body != nil {
		if _, taken := props["body"]; taken {
			b.warn("%s: a parameter is named body; the request body is left out", operation)
		} else {
			schema := asMap(r.schema(operation, mediaSchema(asMap(body["content"]))))
			if schema == nil {
				schema = map[string]any{}
			}
			if d := cleanText(str(body["description"]), maxDescription); d != "" && schema["description"] == nil {
				schema["description"] = d
			}
			props["body"] = schema
			if body["required"] == true {
				required = append(required, "body")
			}
		}
	}
	out := map[string]any{"type": "object", "properties": props, "additionalProperties": false}
	if len(required) > 0 {
		sort.Strings(required)
		req := make([]any, len(required))
		for i, s := range required {
			req[i] = s
		}
		out["required"] = req
	}
	return out
}

// mediaSchema picks the JSON media type's schema, else the first one's.
func mediaSchema(content map[string]any) any {
	if content == nil {
		return nil
	}
	types := sortedKeys(content)
	for _, t := range types {
		mt := strings.ToLower(strings.TrimSpace(strings.SplitN(t, ";", 2)[0]))
		if mt == "application/json" || strings.HasSuffix(mt, "+json") {
			return asMap(content[t])["schema"]
		}
	}
	if len(types) > 0 {
		return asMap(content[types[0]])["schema"]
	}
	return nil
}

func nonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// resolver inlines references inside the document. A reference outside it
// is never followed (it would make the importer fetch a URL); a cycle ends
// in a marker instead of recursing.
type resolver struct {
	doc   map[string]any
	b     *builder
	nodes int
}

// resolveRef follows a top-level reference of an object (a path item, a
// parameter, a request body) without copying the object.
func (r *resolver) resolveRef(op string, v any) any {
	m := asMap(v)
	for range maxRefDepth {
		ref := str(m["$ref"])
		if ref == "" {
			return v
		}
		target, ok := r.lookup(op, ref)
		if !ok {
			return nil
		}
		v = target
		m = asMap(v)
	}
	r.b.warn("%s: references nest deeper than %d", op, maxRefDepth)
	return nil
}

// schema returns a copy of a schema with the document's references inlined.
func (r *resolver) schema(op string, v any) any {
	if v == nil {
		return nil
	}
	r.nodes = 0
	out := r.copy(op, v, nil, 0)
	if r.nodes > maxSchemaNodes {
		r.b.warn("%s: a schema is too large to inline; recorded as an open object", op)
		return map[string]any{"type": "object"}
	}
	return out
}

func (r *resolver) copy(op string, v any, stack []string, depth int) any {
	r.nodes++
	if r.nodes > maxSchemaNodes {
		return nil
	}
	if depth > maxSchemaDepth {
		r.b.warn("%s: a schema nests deeper than %d levels; cut", op, maxSchemaDepth)
		return map[string]any{}
	}
	switch t := v.(type) {
	case map[string]any:
		if ref := str(t["$ref"]); ref != "" {
			for _, s := range stack {
				if s == ref {
					return map[string]any{"x-agenttwin-recursive": ref}
				}
			}
			if len(stack) >= maxRefDepth {
				r.b.warn("%s: references nest deeper than %d", op, maxRefDepth)
				return map[string]any{"x-agenttwin-unresolved": ref}
			}
			target, ok := r.lookup(op, ref)
			if !ok {
				return map[string]any{"x-agenttwin-unresolved": ref}
			}
			resolved, _ := r.copy(op, target, append(stack, ref), depth+1).(map[string]any)
			if resolved == nil {
				resolved = map[string]any{}
			}
			// Keywords beside $ref (OpenAPI 3.1) apply as well; the
			// description beside a reference is the more specific one.
			for k, x := range t {
				if k != "$ref" {
					resolved[k] = r.copy(op, x, stack, depth+1)
				}
			}
			return resolved
		}
		out := make(map[string]any, len(t))
		for k, x := range t {
			if k == "description" || k == "title" {
				if s, ok := x.(string); ok {
					out[k] = cleanText(s, maxDescription)
					continue
				}
			}
			// Examples are data, often real data: they are not kept.
			if k == "example" || k == "examples" {
				continue
			}
			out[k] = r.copy(op, x, stack, depth+1)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = r.copy(op, x, stack, depth+1)
		}
		return out
	}
	return v
}

// lookup resolves a local JSON pointer ("#/components/schemas/Order").
func (r *resolver) lookup(op, ref string) (any, bool) {
	if !strings.HasPrefix(ref, "#/") {
		r.b.warn("%s: reference %s is outside the document; not followed", op, cleanText(ref, 200))
		return nil, false
	}
	var cur any = r.doc
	for _, part := range strings.Split(ref[2:], "/") {
		part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
		if unescaped, err := url.PathUnescape(part); err == nil {
			part = unescaped
		}
		m := asMap(cur)
		if m == nil {
			r.b.warn("%s: reference %s does not resolve", op, cleanText(ref, 200))
			return nil, false
		}
		next, ok := m[part]
		if !ok {
			r.b.warn("%s: reference %s does not resolve", op, cleanText(ref, 200))
			return nil, false
		}
		cur = next
	}
	return cur, true
}
