package policy

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

// The demo seeds these policies and activates them; they must compile, pass
// their tests, leave no rule undecided and be activatable for the tools as
// the demo registers them (the risk its manifest gives each tool).
func TestDemoPoliciesAreActivatable(t *testing.T) {
	dir := filepath.Join("..", "..", "..", "..", "demo", "support-refund-agent", "assurance", "policies")
	risks := map[string]string{"refund_payment": "WRITE_IRREVERSIBLE", "export_customer_data": "ADMIN"}
	files, err := filepath.Glob(filepath.Join(dir, "*.yaml"))
	if err != nil || len(files) != 2 {
		t.Fatalf("demo policies: %v %v", files, err)
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		s, err := Parse(raw)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		c, err := Compile(s)
		if err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		risk, ok := risks[s.Spec.Tool]
		if !ok {
			t.Fatalf("%s guards %s, which the demo does not register", f, s.Spec.Tool)
		}
		if ps := c.ActivationProblems(risk); len(ps) > 0 {
			t.Fatalf("%s is not activatable: %+v", f, ps)
		}
		if u := c.Undecided(); len(u) > 0 {
			t.Fatalf("%s: no test decides %v", f, u)
		}
	}
	// The refund policy asks a person above 100 and refuses a second refund,
	// exactly at its thresholds.
	raw, _ := os.ReadFile(filepath.Join(dir, "refund-limits.yaml"))
	s, _ := Parse(raw)
	c, _ := Compile(s)
	for _, tc := range []struct {
		amount float64
		calls  int
		effect Effect
	}{
		{100, 0, Allow}, {100.01, 0, RequireApproval}, {1000, 0, RequireApproval}, {1000.01, 0, Deny},
		{40, 1, Deny}, {40, 0, Allow},
	} {
		d := c.Decide(Input{Tool: "refund_payment", Args: map[string]any{"amount": tc.amount}, TraceCalls: tc.calls})
		if d.Effect != tc.effect {
			t.Fatalf("amount %v, %d earlier refunds: %s, want %s", tc.amount, tc.calls, d.Effect, tc.effect)
		}
	}
	if !slices.ContainsFunc(c.Boundaries(Input{Tool: "refund_payment", Args: map[string]any{"amount": 40.0}}),
		func(b Boundary) bool { return b.Path == "args.amount" && b.Value == 100 }) {
		t.Fatal("the automatic limit is not a probed threshold")
	}
}
