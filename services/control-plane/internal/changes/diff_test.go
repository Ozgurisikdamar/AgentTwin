package changes

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func schema(t *testing.T, s string) map[string]any {
	t.Helper()
	if s == "" {
		return nil
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestSchemaChanges(t *testing.T) {
	for _, c := range []struct {
		name, a, b string
		want       []string // "path change breaking"
	}{
		{"same", `{"type":"object","properties":{"x":{"type":"string"}}}`, `{"type":"object","properties":{"x":{"type":"string"}}}`, nil},
		{"optional added", `{"properties":{}}`, `{"properties":{"x":{"type":"string"}}}`, []string{"/properties/x property_added false"}},
		{"required added", `{"properties":{}}`, `{"properties":{"x":{}},"required":["x"]}`, []string{"/properties/x required_added true"}},
		{"made required", `{"properties":{"x":{}}}`, `{"properties":{"x":{}},"required":["x"]}`, []string{"/properties/x required_added true"}},
		{"made optional", `{"properties":{"x":{}},"required":["x"]}`, `{"properties":{"x":{}}}`, []string{"/properties/x required_removed false"}},
		{"removed", `{"properties":{"x":{}}}`, `{"properties":{}}`, []string{"/properties/x property_removed true"}},
		{"widened", `{"properties":{"x":{"type":"integer"}}}`, `{"properties":{"x":{"type":"number"}}}`, []string{"/properties/x type_changed false"}},
		{"narrowed", `{"properties":{"x":{"type":"number"}}}`, `{"properties":{"x":{"type":"integer"}}}`, []string{"/properties/x type_changed true"}},
		{"nullable", `{"properties":{"x":{"type":"string"}}}`, `{"properties":{"x":{"type":["string","null"]}}}`, []string{"/properties/x type_changed false"}},
		{"retyped", `{"properties":{"x":{"type":"string"}}}`, `{"properties":{"x":{"type":"object"}}}`, []string{"/properties/x type_changed true"}},
		{"enum grows", `{"properties":{"x":{"enum":["a"]}}}`, `{"properties":{"x":{"enum":["a","b"]}}}`, []string{"/properties/x enum_changed false"}},
		{"enum shrinks", `{"properties":{"x":{"enum":["a","b"]}}}`, `{"properties":{"x":{"enum":["a"]}}}`, []string{"/properties/x enum_changed true"}},
		{"enum appears", `{"properties":{"x":{"type":"string"}}}`, `{"properties":{"x":{"type":"string","enum":["a"]}}}`, []string{"/properties/x enum_changed true"}},
		{"enum goes", `{"properties":{"x":{"enum":["a"]}}}`, `{"properties":{"x":{}}}`, []string{"/properties/x enum_changed false"}},
		{"enum reordered", `{"properties":{"x":{"enum":["a","b"]}}}`, `{"properties":{"x":{"enum":["b","a"]}}}`, nil},
		{"described", `{"properties":{"x":{"description":"old"}}}`, `{"properties":{"x":{"description":"new"}}}`, []string{"/properties/x description_changed false"}},
		{"bounded", `{"properties":{"x":{}}}`, `{"properties":{"x":{"maximum":10}}}`, []string{"/properties/x/maximum constraint_changed true"}},
		{"unbounded", `{"properties":{"x":{"maximum":10}}}`, `{"properties":{"x":{}}}`, []string{"/properties/x/maximum constraint_changed false"}},
		{"int vs float", `{"properties":{"x":{"maximum":10}}}`, `{"properties":{"x":{"maximum":10.0}}}`, nil},
		{"closed", `{"properties":{}}`, `{"properties":{},"additionalProperties":false}`, []string{"/additionalProperties constraint_changed true"}},
		{"nested", `{"properties":{"o":{"properties":{"y":{"type":"string"}}}}}`, `{"properties":{"o":{"properties":{"y":{"type":"integer"}}}}}`,
			[]string{"/properties/o/properties/y type_changed true"}},
		{"array items", `{"properties":{"l":{"items":{"type":"string"}}}}`, `{"properties":{"l":{"items":{"type":"integer"}}}}`,
			[]string{"/properties/l/items type_changed true"}},
		{"schema added", ``, `{"properties":{"x":{}},"required":["x"]}`, []string{" schema_added true"}},
		{"schema added, all optional", ``, `{"properties":{"x":{}}}`, []string{" schema_added false"}},
		{"schema removed", `{"properties":{}}`, ``, []string{" schema_removed false"}},
		{"none", ``, ``, nil},
	} {
		t.Run(c.name, func(t *testing.T) {
			var got []string
			for _, ch := range DiffSchemas(schema(t, c.a), schema(t, c.b)) {
				got = append(got, fmt.Sprintf("%s %s %v", ch.Path, ch.Change, ch.Breaking))
			}
			if strings.Join(got, "|") != strings.Join(c.want, "|") {
				t.Errorf("got %v, want %v", got, c.want)
			}
		})
	}
}

// A manifest decoded from YAML holds ints where one decoded from JSON holds
// floats: the same number is not a change.
func TestNumbersCompareByValue(t *testing.T) {
	a := map[string]any{"properties": map[string]any{"x": map[string]any{"maximum": 10, "enum": []any{1, 2}}}}
	b := map[string]any{"properties": map[string]any{"x": map[string]any{"maximum": 10.0, "enum": []any{2.0, 1.0}}}}
	if got := DiffSchemas(a, b); len(got) != 0 {
		t.Errorf("10 vs 10.0: %v", got)
	}
}

// A schema is data from a manifest: its depth and size are not trusted.
func TestSchemaDiffIsBounded(t *testing.T) {
	deep := func(leaf string) map[string]any {
		node := map[string]any{"type": leaf}
		for range 500 {
			node = map[string]any{"properties": map[string]any{"n": node}}
		}
		return node
	}
	if got := DiffSchemas(deep("string"), deep("integer")); len(got) != 0 {
		t.Errorf("a change below the depth bound was reported: %v", got)
	}
	wide := func(n int) map[string]any {
		props := map[string]any{}
		for i := range n {
			props[fmt.Sprintf("p%04d", i)] = map[string]any{}
		}
		return map[string]any{"properties": props}
	}
	if got := DiffSchemas(wide(0), wide(5000)); len(got) != maxSchemaChanges {
		t.Errorf("%d changes reported, bound %d", len(got), maxSchemaChanges)
	}
}

func TestTextDiff(t *testing.T) {
	lines, truncated, ok := DiffText("a\nb\nc\nd\ne\nf\ng\nh", "a\nb\nc\nd\nX\nf\ng\nh")
	var got []string
	for _, l := range lines {
		got = append(got, l.Op+l.Text)
	}
	// Two lines of context around the change, nothing else.
	if strings.Join(got, ",") != " c, d,-e,+X, f, g" || truncated || !ok {
		t.Errorf("diff = %v (truncated %v ok %v)", got, truncated, ok)
	}
	if lines, _, ok := DiffText("same\n", "same"); !ok || len(lines) != 0 {
		t.Errorf("identical texts: %v", lines)
	}
	if lines, _, _ := DiffText("", "new"); len(lines) != 1 || lines[0] != (DiffLine{"+", "new"}) {
		t.Errorf("from nothing: %v", lines)
	}
	if lines, _, _ := DiffText("a\r\nb", "a\nb"); len(lines) != 0 {
		t.Errorf("line endings are not a change: %v", lines)
	}
	big := strings.Repeat("line\n", maxDiffInputLines+1)
	if _, _, ok := DiffText(big, "x"); ok {
		t.Error("a text past the input bound was diffed")
	}
	var many []string
	for i := range 1000 {
		many = append(many, fmt.Sprintf("l%d", i))
	}
	lines, truncated, ok = DiffText("", strings.Join(many, "\n"))
	if !ok || !truncated || len(lines) != maxDiffOutputLines {
		t.Errorf("output bound: %d lines, truncated %v", len(lines), truncated)
	}
}
