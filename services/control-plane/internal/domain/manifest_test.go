package domain

import (
	"errors"
	"os"
	"strings"
	"testing"
)

func readDemo(t *testing.T, v string) []byte {
	t.Helper()
	b, err := os.ReadFile("../../../../demo/support-refund-agent/manifests/" + v + ".yaml")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestDemoManifestsAreValid(t *testing.T) {
	for _, v := range []string{"1.2.3", "1.2.4", "1.3.0", "1.3.1", "1.3.2"} {
		m, err := ParseManifest(readDemo(t, v))
		if err != nil {
			t.Fatalf("%s: %v", v, err)
		}
		if m.Name != "support-refund-agent" || m.Version != v {
			t.Fatalf("%s: metadata %s@%s", v, m.Name, m.Version)
		}
		refund, ok := m.Tool("refund_payment")
		if !ok || refund.Risk != RiskWriteIrreversible || refund.ApprovalWhen != "args.amount > 100" {
			t.Fatalf("%s: refund tool %+v", v, refund)
		}
		if m.ContentMode != "redacted" || m.PromptSHA256 == "" || m.RuntimeEndpoint == "" {
			t.Fatalf("%s: normalized fields %+v", v, m)
		}
		if len(m.Dependencies) != 4 {
			t.Fatalf("%s: dependencies %+v", v, m.Dependencies)
		}
	}
}

func TestManifestHashTracksPromptButNotFormatting(t *testing.T) {
	a, _ := ParseManifest(readDemo(t, "1.2.4"))
	b, _ := ParseManifest(readDemo(t, "1.3.0"))
	if a.PromptSHA256 == b.PromptSHA256 {
		t.Fatal("prompt change must change prompt hash")
	}
	if a.Hash() == b.Hash() {
		t.Fatal("prompt change must change manifest hash")
	}
	// Tool definitions are unchanged between the two versions.
	ra, _ := a.Tool("refund_payment")
	rb, _ := b.Tool("refund_payment")
	if ra.DefinitionSHA != rb.DefinitionSHA {
		t.Fatal("unchanged tool must keep its definition hash")
	}
	// Re-serializing as JSON must yield the same identity as the YAML source.
	jsonDoc := `{"apiVersion":"agenttwin.dev/v1","kind":"Agent","metadata":{"name":"a","version":"1.0.0"},"spec":{"model":{"provider":"p","name":"m"},"tools":[{"name":"t","risk":"READ"}]}}`
	yamlDoc := "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {version: 1.0.0, name: a}\nspec:\n  tools: [{risk: READ, name: t}]\n  model: {name: m, provider: p}\n"
	mj, err := ParseManifest([]byte(jsonDoc))
	if err != nil {
		t.Fatal(err)
	}
	my, err := ParseManifest([]byte(yamlDoc))
	if err != nil {
		t.Fatal(err)
	}
	if mj.Hash() != my.Hash() {
		t.Fatal("equivalent JSON and YAML manifests must hash identically")
	}
}

func TestManifestDefaultsAreConservative(t *testing.T) {
	m, err := ParseManifest([]byte("apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 0.1.0}\nspec:\n  model: {provider: p, name: m}\n  tools: [{name: mystery_tool}]\n"))
	if err != nil {
		t.Fatal(err)
	}
	tool, _ := m.Tool("mystery_tool")
	if tool.Risk != RiskWriteIrreversible || tool.RiskDeclared {
		t.Fatalf("undeclared risk must default to WRITE_IRREVERSIBLE: %+v", tool)
	}
	if m.ContentMode != "off" {
		t.Fatalf("content capture must default to off, got %s", m.ContentMode)
	}
}

func TestManifestValidationErrors(t *testing.T) {
	cases := map[string]string{
		"wrong api version": "apiVersion: v2\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: []}\n",
		"bad semver":        "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: one}\nspec: {model: {provider: p, name: m}, tools: []}\n",
		"bad risk":          "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: [{name: t, risk: YOLO}]}\n",
		"duplicate tool":    "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: [{name: t}, {name: t}]}\n",
		"unknown field":     "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: [], shell: rm}\n",
		"bad name":          "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: '../etc', version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: []}\n",
		"undeclared dep":    "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: [], dependencies: [{tool: x, dependsOn: [{kind: SERVICE, name: s}]}]}\n",
		"yaml alias":        "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: &m {name: a, version: 1.0.0}\nx: *m\n",
		"not an object":     "- 1\n- 2\n",
		"empty":             "",
	}
	for name, doc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := ParseManifest([]byte(doc))
			var me *ManifestError
			if !errors.As(err, &me) || len(me.Problems) == 0 {
				t.Fatalf("expected ManifestError, got %v", err)
			}
		})
	}
}

func TestManifestRejectsOversizedAndDeepDocuments(t *testing.T) {
	big := "apiVersion: agenttwin.dev/v1\n# " + strings.Repeat("x", MaxManifestBytes) + "\n"
	if _, err := ParseManifest([]byte(big)); err == nil {
		t.Fatal("oversized document must be rejected")
	}
	deep := strings.Repeat("[", 200) + strings.Repeat("]", 200)
	if _, err := ParseManifest([]byte("a: " + deep)); err == nil {
		t.Fatal("deeply nested document must be rejected")
	}
}

// Manifests are stored as jsonb, which holds no NUL character: one written
// with an escape (YAML "\0", JSON "\u0000") is refused as a manifest problem,
// not left to fail inside the database.
func TestManifestTextMustBeStorable(t *testing.T) {
	valid := "apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0, description: \"çağrı 注文\"}\n" +
		"spec: {model: {provider: p, name: m}, tools: []}\n"
	if _, err := ParseManifest([]byte(valid)); err != nil {
		t.Fatalf("a manifest with non-ASCII text: %v", err)
	}
	for _, doc := range []string{
		strings.Replace(valid, "çağrı", `refund\0`, 1),
		strings.Replace(valid, "name: a,", `name: "a\x00",`, 1),
		`{"apiVersion":"agenttwin.dev/v1","kind":"Agent","metadata":{"name":"a","version":"1.0.0","description":"x\u0000"},` +
			`"spec":{"model":{"provider":"p","name":"m"},"tools":[]}}`,
	} {
		_, err := ParseManifest([]byte(doc))
		var me *ManifestError
		if !errors.As(err, &me) || !strings.Contains(err.Error(), "NUL") {
			t.Errorf("%q: %v", doc, err)
		}
	}
}

func FuzzParseManifestNeverPanics(f *testing.F) {
	f.Add([]byte("apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: a, version: 1.0.0}\nspec: {model: {provider: p, name: m}, tools: []}\n"))
	f.Add([]byte(`{"apiVersion":"agenttwin.dev/v1"}`))
	f.Add([]byte("{{{"))
	f.Fuzz(func(t *testing.T, data []byte) {
		_, _ = ParseManifest(data)
	})
}
