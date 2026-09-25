package changes

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"sort"
)

// SchemaChange is one difference between two versions of a tool's input
// schema (spec §118).
type SchemaChange struct {
	// Path locates the changed node, JSON-Pointer-like from the root
	// ("/properties/amount").
	Path string `json:"path"`
	// Change is required_added, required_removed, property_added,
	// property_removed, type_changed, enum_changed, description_changed,
	// constraint_changed or schema_added / schema_removed.
	Change string `json:"change"`
	// Breaking marks a change a caller written for the old schema can trip
	// over: a new required argument, a removed argument, a narrowed type or
	// enum, a tightened constraint.
	Breaking bool `json:"breaking"`
	From     any  `json:"from,omitempty"`
	To       any  `json:"to,omitempty"`
}

// maxSchemaDepth bounds the recursion into nested schemas (a schema is data
// supplied by a manifest; its depth is not trusted).
const maxSchemaDepth = 12

// maxSchemaChanges bounds the changes reported for one tool.
const maxSchemaChanges = 200

// constraints are the keywords whose change narrows or widens what is
// accepted; only their difference is reported.
var constraints = []string{"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
	"pattern", "format", "minItems", "maxItems", "multipleOf", "const", "additionalProperties"}

// DiffSchemas compares two JSON Schemas. It reports what a model calling the
// tool can notice: arguments, their types, enums, descriptions (the model
// reads them) and constraints. Nil schemas are "no schema".
func DiffSchemas(base, candidate map[string]any) []SchemaChange {
	var out []SchemaChange
	switch {
	case base == nil && candidate == nil:
		return nil
	case base == nil:
		return []SchemaChange{{Path: "", Change: "schema_added", Breaking: len(required(candidate)) > 0}}
	case candidate == nil:
		return []SchemaChange{{Path: "", Change: "schema_removed"}}
	}
	diffNode(&out, "", base, candidate, 0)
	if len(out) > maxSchemaChanges {
		out = out[:maxSchemaChanges]
	}
	return out
}

func diffNode(out *[]SchemaChange, path string, a, b map[string]any, depth int) {
	if depth > maxSchemaDepth || len(*out) > maxSchemaChanges {
		return
	}
	add := func(c SchemaChange) { *out = append(*out, c) }
	if ta, tb := typesOf(a), typesOf(b); !slices.Equal(ta, tb) && (len(ta) > 0 || len(tb) > 0) {
		// Widening (a type added, none removed) accepts every old value.
		add(SchemaChange{Path: path, Change: "type_changed", Breaking: !subset(ta, tb), From: typeValue(ta), To: typeValue(tb)})
	}
	if ea, eb := listOf(a["enum"]), listOf(b["enum"]); ea != nil || eb != nil {
		if !sameValues(ea, eb) {
			// A value removed, or an enum appearing where any value was
			// accepted, narrows what the tool accepts.
			breaking := eb != nil && (ea == nil || len(valuesMinus(ea, eb)) > 0)
			add(SchemaChange{Path: path, Change: "enum_changed", Breaking: breaking, From: ea, To: eb})
		}
	}
	if da, db := str(a["description"]), str(b["description"]); da != db {
		add(SchemaChange{Path: path, Change: "description_changed", From: da, To: db})
	}
	for _, k := range constraints {
		va, okA := a[k]
		vb, okB := b[k]
		if okA == okB && (!okA || reflect.DeepEqual(norm(va), norm(vb))) {
			continue
		}
		// A constraint appearing or changing may reject what passed; one
		// disappearing only accepts more.
		add(SchemaChange{Path: path + "/" + k, Change: "constraint_changed", Breaking: okB, From: va, To: vb})
	}

	pa, pb := props(a), props(b)
	ra, rb := required(a), required(b)
	for _, name := range sortedKeys(pa, pb) {
		p := path + "/properties/" + name
		sa, inA := pa[name]
		sb, inB := pb[name]
		switch {
		case inA && !inB:
			add(SchemaChange{Path: p, Change: "property_removed", Breaking: true})
		case !inA && inB:
			if slices.Contains(rb, name) {
				add(SchemaChange{Path: p, Change: "required_added", Breaking: true})
			} else {
				add(SchemaChange{Path: p, Change: "property_added"})
			}
		default:
			switch {
			case !slices.Contains(ra, name) && slices.Contains(rb, name):
				add(SchemaChange{Path: p, Change: "required_added", Breaking: true})
			case slices.Contains(ra, name) && !slices.Contains(rb, name):
				add(SchemaChange{Path: p, Change: "required_removed"})
			}
			diffNode(out, p, sa, sb, depth+1)
		}
	}
	ia, ib := asMap(a["items"]), asMap(b["items"])
	switch {
	case ia != nil && ib != nil:
		diffNode(out, path+"/items", ia, ib, depth+1)
	case ia != nil || ib != nil:
		add(SchemaChange{Path: path + "/items", Change: "constraint_changed", Breaking: ib != nil, From: ia != nil, To: ib != nil})
	}
}

func typesOf(s map[string]any) []string {
	switch t := s["type"].(type) {
	case string:
		return []string{t}
	case []any:
		var out []string
		for _, v := range t {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
		sort.Strings(out)
		return out
	}
	return nil
}

func typeValue(ts []string) any {
	switch len(ts) {
	case 0:
		return nil
	case 1:
		return ts[0]
	}
	return ts
}

// subset reports whether every type of a is still accepted by b. "integer"
// is accepted by "number"; no type at all accepts everything.
func subset(a, b []string) bool {
	if len(b) == 0 {
		return true
	}
	if len(a) == 0 {
		return false
	}
	for _, t := range a {
		accepted := slices.Contains(b, t) || (t == "integer" && slices.Contains(b, "number"))
		if !accepted {
			return false
		}
	}
	return true
}

func props(s map[string]any) map[string]map[string]any {
	out := map[string]map[string]any{}
	if m, ok := s["properties"].(map[string]any); ok {
		for k, v := range m {
			if vm, ok := v.(map[string]any); ok {
				out[k] = vm
			} else {
				out[k] = map[string]any{}
			}
		}
	}
	return out
}

func required(s map[string]any) []string {
	var out []string
	for _, v := range listOf(s["required"]) {
		if name, ok := v.(string); ok {
			out = append(out, name)
		}
	}
	return out
}

func sortedKeys(a, b map[string]map[string]any) []string {
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
	return keys
}

func listOf(v any) []any {
	l, _ := v.([]any)
	return l
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

// norm makes numbers comparable whatever decoded them (float64 from JSON,
// int from YAML).
func norm(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprint(v)
	}
	var out any
	_ = json.Unmarshal(b, &out)
	return out
}

func key(v any) string {
	b, _ := json.Marshal(norm(v))
	return string(b)
}

func sameValues(a, b []any) bool {
	return len(valuesMinus(a, b)) == 0 && len(valuesMinus(b, a)) == 0
}

// valuesMinus returns the values of a not in b, in a's order.
func valuesMinus(a, b []any) []any {
	in := map[string]bool{}
	for _, v := range b {
		in[key(v)] = true
	}
	out := []any{}
	for _, v := range a {
		if !in[key(v)] {
			out = append(out, v)
		}
	}
	return out
}
