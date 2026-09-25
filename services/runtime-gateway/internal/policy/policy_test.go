package policy

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// refundPolicy is the demo's refund policy (ADR-0033): a refund must name a
// valid order, happens once per conversation, never above 500 and above 100
// only with a person's approval.
const refundPolicy = `
apiVersion: agenttwin.dev/v1
kind: Policy
metadata:
  name: refund-limits
  description: Refunds the support agent may issue on its own.
spec:
  tool: refund_payment
  rules:
    - name: valid-order
      when: "!has(args.order_id) || !string(args.order_id).matches('^ORD-[0-9]+$')"
      effect: deny
      message: A refund must name a valid order.
    - name: one-refund-per-conversation
      when: trace.calls >= 1
      effect: deny
      message: One refund per conversation.
    - name: never-above-500
      when: args.amount > 500
      effect: deny
      message: Refunds above 500 are never automatic.
    - name: approval-above-100
      when: args.amount > 100
      effect: require_approval
      message: Refunds above 100 need a person's approval.
  approval:
    expiresInSeconds: 900
  tests:
    - name: small refund
      args: {order_id: ORD-1001, amount: 40}
      expect: allow
    - name: over the automatic limit
      args: {order_id: ORD-1003, amount: 150}
      expect: require_approval
      rule: approval-above-100
    - name: far above the limit
      args: {order_id: ORD-1003, amount: 900}
      expect: deny
      rule: never-above-500
    - name: a second refund
      args: {order_id: ORD-1001, amount: 40}
      context: {traceCalls: 1}
      expect: deny
      rule: one-refund-per-conversation
    - name: no order
      args: {amount: 40}
      expect: deny
      rule: valid-order
`

func mustCompile(t *testing.T, doc string) *Compiled {
	t.Helper()
	s, err := Parse([]byte(doc))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	c, err := Compile(s)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return c
}

// minimal builds a one-tool document from rule lines and extra spec lines.
func minimal(rules string, extra ...string) string {
	return "apiVersion: agenttwin.dev/v1\nkind: Policy\nmetadata: {name: p}\nspec:\n  tool: t\n" +
		strings.Join(extra, "") + "  rules:\n" + rules
}

func problemFields(t *testing.T, err error) []string {
	t.Helper()
	var inv *InvalidError
	if !errors.As(err, &inv) {
		t.Fatalf("expected an InvalidError, got %v", err)
	}
	fields := make([]string, len(inv.Problems))
	for i, p := range inv.Problems {
		fields[i] = p.Field
	}
	return fields
}

// ---------------------------------------------------------------- documents

func TestParseFillsDefaultsAndReadsYAMLAndJSONAlike(t *testing.T) {
	y, err := Parse([]byte(refundPolicy))
	if err != nil {
		t.Fatal(err)
	}
	if y.Spec.Default != Allow || y.Spec.FailMode != FailClosed {
		t.Fatalf("defaults: %q %q", y.Spec.Default, y.Spec.FailMode)
	}
	if y.ApprovalExpiry() != 900 || len(y.Spec.Rules) != 4 || len(y.Spec.Tests) != 5 {
		t.Fatalf("read: %+v", y.Spec)
	}
	if y.Spec.Tests[3].Context.TraceCalls != 1 || y.Spec.Tests[0].Args["amount"] != 40.0 {
		t.Fatalf("tests: %+v", y.Spec.Tests)
	}
	j := `{"kind":"Policy","apiVersion":"agenttwin.dev/v1","metadata":{"name":"p"},
	  "spec":{"rules":[{"effect":"deny","when":"args.amount > 5","name":"r"}],"tool":"t"}}`
	fromJSON, err := Parse([]byte(j))
	if err != nil {
		t.Fatal(err)
	}
	fromYAML, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n")))
	if err != nil {
		t.Fatal(err)
	}
	if fromJSON.Hash() != fromYAML.Hash() {
		t.Fatal("the same policy in JSON and YAML hashes differently")
	}
	if len(fromJSON.Hash()) != 64 {
		t.Fatalf("hash %q", fromJSON.Hash())
	}
	explicit, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n",
		"  default: allow\n  failMode: fail_closed\n")))
	if err != nil {
		t.Fatal(err)
	}
	if explicit.Hash() != fromYAML.Hash() {
		t.Fatal("filled defaults must hash like explicit ones")
	}
	changed, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 6, effect: deny}\n")))
	if err != nil {
		t.Fatal(err)
	}
	if changed.Hash() == fromYAML.Hash() {
		t.Fatal("a changed rule must change the hash")
	}
	if (Spec{}).ApprovalExpiry() != DefaultApprovalExpiry {
		t.Fatal("default approval expiry")
	}
	short, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n", "  approval: {expiresInSeconds: 300}\n")))
	if err != nil || short.ApprovalExpiry() != 300 {
		t.Fatalf("approval expiry: %v %v", short.ApprovalExpiry(), err)
	}
	withArgs, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n", "  tests:\n    - {name: x, args: {}, expect: allow}\n")))
	if err != nil {
		t.Fatal(err)
	}
	noArgs, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n", "  tests:\n    - {name: x, expect: allow}\n")))
	if err != nil {
		t.Fatal(err)
	}
	if noArgs.Spec.Tests[0].Args == nil || noArgs.Hash() != withArgs.Hash() {
		t.Fatal("a test without args has empty args, and hashes like one with {}")
	}
	numericKeys, err := Parse([]byte(minimal("    - {name: r, when: args.amount > 5, effect: deny}\n", "  tests:\n    - {name: x, args: {1: 2, nested: {3: 4}}, expect: allow}\n")))
	if err != nil || numericKeys.Spec.Tests[0].Args["1"] != 2.0 || numericKeys.Spec.Tests[0].Args["nested"].(map[string]any)["3"] != 4.0 {
		t.Fatalf("YAML keys that are not strings: %+v %v", numericKeys.Spec.Tests, err)
	}
}

func TestParseRefusesWhatItCannotTrust(t *testing.T) {
	rule := "    - {name: r, when: args.a > 1, effect: deny}\n"
	cases := map[string]struct {
		doc    string
		fields []string
	}{
		"empty":            {"  \n", []string{""}},
		"not yaml":         {"spec: [unclosed", []string{""}},
		"unknown field":    {minimal("    - {name: r, when: args.a > 1, efect: deny}\n"), []string{""}},
		"two documents":    {minimal(rule) + "---\n" + minimal(rule), []string{""}},
		"two json values":  {`{"kind":"Policy"} {"kind":"Policy"}`, []string{""}},
		"wrong identity":   {strings.Replace(strings.Replace(minimal(rule), "agenttwin.dev/v1", "v0", 1), "kind: Policy", "kind: Agent", 1), []string{"apiVersion", "kind"}},
		"bad names":        {strings.Replace(minimal("    - {name: Bad Name, when: args.a > 1, effect: deny}\n"), "name: p}", "name: P_1}", 1), []string{"metadata.name", "spec.rules[0].name"}},
		"no rules":         {minimal(""), []string{"spec.rules"}},
		"duplicate rule":   {minimal(rule + rule), []string{"spec.rules[1].name"}},
		"empty condition":  {minimal("    - {name: r, when: ' ', effect: deny}\n"), []string{"spec.rules[0].when"}},
		"long condition":   {minimal("    - {name: r, when: '" + strings.Repeat("a", MaxExpression+1) + "', effect: deny}\n"), []string{"spec.rules[0].when"}},
		"bad effect":       {minimal("    - {name: r, when: args.a > 1, effect: block}\n"), []string{"spec.rules[0].effect"}},
		"long message":     {minimal("    - {name: r, when: args.a > 1, effect: deny, message: '" + strings.Repeat("m", MaxMessage+1) + "'}\n"), []string{"spec.rules[0].message"}},
		"bad tool":         {strings.Replace(minimal(rule), "tool: t", "tool: 'refund payment'", 1), []string{"spec.tool"}},
		"bad default":      {minimal(rule, "  default: maybe\n"), []string{"spec.default"}},
		"bad fail mode":    {minimal(rule, "  failMode: shrug\n"), []string{"spec.failMode"}},
		"empty limits":     {minimal(rule, "  limits: {}\n"), []string{"spec.limits"}},
		"limits too big":   {minimal(rule, "  limits: {timeoutMs: 60001, maxResponseBytes: -1}\n"), []string{"spec.limits.timeoutMs", "spec.limits.maxResponseBytes"}},
		"expiry too short": {minimal(rule, "  approval: {expiresInSeconds: 59}\n"), []string{"spec.approval.expiresInSeconds"}},
		"expiry too long":  {minimal(rule, "  approval: {expiresInSeconds: 86401}\n"), []string{"spec.approval.expiresInSeconds"}},
		"long description": {strings.Replace(minimal(rule), "name: p}", "name: p, description: '"+strings.Repeat("d", MaxDescription+1)+"'}", 1), []string{"metadata.description"}},
		"bad tests": {minimal(rule, "  tests:\n    - {name: a, expect: allow}\n    - {name: a, expect: nope, rule: ghost, context: {traceCalls: -1}}\n    - {name: '', expect: allow}\n"),
			[]string{"spec.tests[1].name", "spec.tests[1].expect", "spec.tests[1].rule", "spec.tests[1].context.traceCalls", "spec.tests[2].name"}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := Parse([]byte(tc.doc))
			if got := problemFields(t, err); !reflect.DeepEqual(got, tc.fields) {
				t.Fatalf("fields %v, want %v (%v)", got, tc.fields, err)
			}
		})
	}
	many := minimal(strings.Repeat(rule, 1))
	for i := range MaxRules {
		many += "    - {name: r" + strings.Repeat("x", i+1) + ", when: args.a > 1, effect: deny}\n"
	}
	if got := problemFields(t, func() error { _, err := Parse([]byte(many)); return err }()); !reflect.DeepEqual(got, []string{"spec.rules"}) {
		t.Fatalf("too many rules: %v", got)
	}
	var tests strings.Builder
	tests.WriteString("  tests:\n")
	for i := range MaxTests + 1 {
		tests.WriteString("    - {name: t" + strings.Repeat("x", i) + ", expect: allow}\n")
	}
	if got := problemFields(t, func() error { _, err := Parse([]byte(minimal(rule, tests.String()))); return err }()); !reflect.DeepEqual(got, []string{"spec.tests"}) {
		t.Fatalf("too many tests: %v", got)
	}
	if _, err := Parse(make([]byte, maxDocument+1)); err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("oversized: %v", err)
	}
	var inv *InvalidError
	_, err := Parse([]byte(minimal("")))
	if !errors.As(err, &inv) || !strings.Contains(err.Error(), "spec.rules: a policy needs at least one rule") {
		t.Fatalf("message: %v", err)
	}
}

func TestCompileTypeChecksEveryRule(t *testing.T) {
	_, err := Compile(func() Spec {
		s, err := Parse([]byte(minimal(
			"    - {name: ok, when: args.amount > 5, effect: deny}\n" +
				"    - {name: unknown-var, when: amount > 5, effect: deny}\n" +
				"    - {name: not-bool, when: args.amount, effect: deny}\n" +
				"    - {name: syntax, when: 'args.amount >', effect: deny}\n" +
				"    - {name: mistyped, when: risk == 1, effect: deny}\n")))
		if err != nil {
			t.Fatal(err)
		}
		return s
	}())
	var inv *InvalidError
	if !errors.As(err, &inv) {
		t.Fatalf("expected InvalidError, got %v", err)
	}
	want := []string{"spec.rules[1].when", "spec.rules[2].when", "spec.rules[3].when", "spec.rules[4].when"}
	if got := problemFields(t, err); !reflect.DeepEqual(got, want) {
		t.Fatalf("fields %v", got)
	}
	if !strings.Contains(inv.Problems[0].Message, "undeclared reference to 'amount'") ||
		!strings.Contains(inv.Problems[0].Message, "unknown-var:1:1") {
		t.Fatalf("unknown variable message: %q", inv.Problems[0].Message)
	}
	if !strings.Contains(inv.Problems[1].Message, "must be a boolean expression") {
		t.Fatalf("non-boolean message: %q", inv.Problems[1].Message)
	}
	if _, err := Compile(Spec{}); err == nil {
		t.Fatal("an invalid document must not compile")
	}
	for _, v := range []string{"tool", "risk", "agent", "agent_version", "environment", "subject", "trace.id", "trace.calls"} {
		if _, err := Compile(func() Spec {
			s, _ := Parse([]byte(minimal("    - {name: r, when: \"string(" + v + ") != ''\", effect: deny}\n")))
			return s
		}()); err != nil {
			t.Fatalf("variable %s is not declared: %v", v, err)
		}
	}
}

// ---------------------------------------------------------------- decisions

func TestTheRefundPolicyDecides(t *testing.T) {
	c := mustCompile(t, refundPolicy)
	cases := []struct {
		name    string
		in      Input
		effect  Effect
		rule    string
		matched []string
		failed  bool
	}{
		{"small", Input{Args: map[string]any{"order_id": "ORD-1", "amount": 40.0}}, Allow, "", nil, false},
		{"at the limit", Input{Args: map[string]any{"order_id": "ORD-1", "amount": 100.0}}, Allow, "", nil, false},
		{"a cent above", Input{Args: map[string]any{"order_id": "ORD-1", "amount": 100.01}}, RequireApproval, "approval-above-100", []string{"approval-above-100"}, false},
		{"far above", Input{Args: map[string]any{"order_id": "ORD-1", "amount": 900.0}}, Deny, "never-above-500", []string{"never-above-500", "approval-above-100"}, false},
		{"second call", Input{TraceCalls: 1, Args: map[string]any{"order_id": "ORD-1", "amount": 150.0}}, Deny, "one-refund-per-conversation", []string{"one-refund-per-conversation", "approval-above-100"}, false},
		{"bad order", Input{Args: map[string]any{"order_id": "1; drop", "amount": 40.0}}, Deny, "valid-order", []string{"valid-order"}, false},
		// The amount rules fail on a missing amount, but a matched deny wins.
		{"nothing", Input{}, Deny, "valid-order", []string{"valid-order"}, false},
		// A missing amount fails two rules: the policy fails closed.
		{"no amount", Input{Args: map[string]any{"order_id": "ORD-1"}}, Deny, "never-above-500", nil, true},
		{"integer amount", Input{Args: map[string]any{"order_id": "ORD-1", "amount": 150}}, RequireApproval, "approval-above-100", []string{"approval-above-100"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := c.Decide(tc.in)
			if d.Effect != tc.effect || d.Rule != tc.rule || d.FailModeApplied != tc.failed || d.Policy != "refund-limits" {
				t.Fatalf("decision %+v", d)
			}
			var got []string
			for _, m := range d.Matched {
				got = append(got, m.Rule)
			}
			if !reflect.DeepEqual(got, tc.matched) {
				t.Fatalf("matched %v, want %v", got, tc.matched)
			}
			if d.Message == "" || d.Limits != nil {
				t.Fatalf("message/limits %+v", d)
			}
		})
	}
	d := c.Decide(Input{Args: map[string]any{"order_id": "ORD-1", "amount": 150.0}})
	if d.Message != "Refunds above 100 need a person's approval." || d.Matched[0].Message != d.Message {
		t.Fatalf("message %q", d.Message)
	}
	d = c.Decide(Input{Args: map[string]any{"order_id": "ORD-1"}})
	if len(d.Errors) != 2 || d.Errors[0].Rule != "never-above-500" || !strings.Contains(d.Errors[0].Error, "no such key") ||
		!strings.Contains(d.Message, "could not be evaluated") || !strings.Contains(d.Message, "fails closed") {
		t.Fatalf("fail closed: %+v", d)
	}
	if d := c.Decide(Input{Args: map[string]any{"order_id": "ORD-1", "amount": 1.0}}); d.Message != "No rule matched; the policy's default is allow." {
		t.Fatalf("default message %q", d.Message)
	}
}

func TestFailModes(t *testing.T) {
	rules := "    - {name: broken, when: args.missing > 1, effect: allow}\n" +
		"    - {name: slow, when: args.n > 10, effect: require_approval}\n"
	in := func(risk string, n float64) Input { return Input{Risk: risk, Args: map[string]any{"n": n}} }
	approval := mustCompile(t, minimal(rules, "  failMode: require_approval\n"))
	if d := approval.Decide(in("WRITE_IRREVERSIBLE", 1)); d.Effect != RequireApproval || d.Rule != "broken" || !d.FailModeApplied ||
		!strings.Contains(d.Message, "asks for approval") {
		t.Fatalf("require_approval fail mode: %+v", d)
	}
	open := mustCompile(t, minimal(rules, "  failMode: fail_open\n"))
	if d := open.Decide(in(RiskRead, 1)); d.Effect != Allow || d.Rule != "" || !d.FailModeApplied {
		t.Fatalf("fail_open on READ proceeds with the other rules: %+v", d)
	}
	if d := open.Decide(in(RiskRead, 20)); d.Effect != RequireApproval || d.Rule != "slow" || !d.FailModeApplied {
		t.Fatalf("fail_open on READ keeps a matched rule: %+v", d)
	}
	if d := open.Decide(in("WRITE_REVERSIBLE", 1)); d.Effect != Deny || d.Rule != "broken" || !d.FailModeApplied ||
		!strings.Contains(d.Message, "fail_open applies only to READ tools") {
		t.Fatalf("fail_open elsewhere fails closed: %+v", d)
	}
	closed := mustCompile(t, minimal(rules))
	if d := closed.Decide(in(RiskRead, 1)); d.Effect != Deny || !d.FailModeApplied {
		t.Fatalf("fail_closed: %+v", d)
	}
	denyWins := mustCompile(t, minimal(rules+"    - {name: always, when: 'true', effect: deny}\n", "  failMode: fail_open\n"))
	if d := denyWins.Decide(in(RiskRead, 1)); d.Effect != Deny || d.Rule != "always" || d.FailModeApplied {
		t.Fatalf("a matched deny wins over a failed rule: %+v", d)
	}
}

func TestTheMostRestrictiveMatchWinsAndTheFirstRuleReportsIt(t *testing.T) {
	c := mustCompile(t, minimal(
		"    - {name: limited, when: 'true', effect: allow_with_limits}\n"+
			"    - {name: first-approval, when: args.n > 1, effect: require_approval}\n"+
			"    - {name: second-approval, when: args.n > 2, effect: require_approval}\n"+
			"    - {name: allowed, when: 'true', effect: allow}\n",
		"  limits: {timeoutMs: 500}\n"))
	d := c.Decide(Input{Args: map[string]any{"n": 5.0}})
	if d.Effect != RequireApproval || d.Rule != "first-approval" || len(d.Matched) != 4 || d.Limits != nil {
		t.Fatalf("decision %+v", d)
	}
	d = c.Decide(Input{Args: map[string]any{"n": 0.0}})
	if d.Effect != AllowWithLimits || d.Rule != "limited" || d.Limits == nil || d.Limits.TimeoutMs != 500 {
		t.Fatalf("limits %+v", d)
	}
	d.Limits.TimeoutMs = 1
	if c.Spec.Spec.Limits.TimeoutMs != 500 {
		t.Fatal("a decision must not share the policy's limits")
	}
	if d := mustCompile(t, minimal("    - {name: r, when: 'false', effect: allow}\n", "  default: deny\n")).Decide(Input{}); d.Effect != Deny || d.Rule != "" {
		t.Fatalf("default deny: %+v", d)
	}
	noLimits := mustCompile(t, minimal("    - {name: r, when: 'true', effect: allow_with_limits}\n"))
	if d := noLimits.Decide(Input{}); d.Effect != AllowWithLimits || d.Limits != nil || d.Message != "Rule r: allow_with_limits." {
		t.Fatalf("allow_with_limits without limits: %+v", d)
	}
}

func TestACostlyRuleFailsToEvaluate(t *testing.T) {
	c := mustCompile(t, minimal("    - {name: heavy, when: 'args.items.all(x, x > 0)', effect: allow}\n"))
	items := make([]any, 200_000)
	for i := range items {
		items[i] = 1.0
	}
	d := c.Decide(Input{Args: map[string]any{"items": items}})
	if d.Effect != Deny || !d.FailModeApplied || !strings.Contains(d.Errors[0].Error, "cost limit") {
		t.Fatalf("cost limit: %+v", d)
	}
	if d := c.Decide(Input{Args: map[string]any{"items": []any{1.0, 2.0}}}); d.Effect != Allow || d.Rule != "heavy" {
		t.Fatalf("cheap input: %+v", d)
	}
}

func TestCombine(t *testing.T) {
	if o := Combine(nil); o.Effect != Allow || o.Message != NoPolicy || o.Policy != "" || o.Decisions == nil {
		t.Fatalf("no policy: %+v", o)
	}
	ds := []Decision{
		{Policy: "a", Effect: AllowWithLimits, Rule: "ra", Message: "a", Limits: &Limits{TimeoutMs: 800, MaxResponseBytes: 100}},
		{Policy: "b", Effect: Allow, Message: "b"},
		{Policy: "c", Effect: AllowWithLimits, Rule: "rc", Message: "c", Limits: &Limits{TimeoutMs: 300}},
		{Policy: "d", Effect: AllowWithLimits, Rule: "rd", Message: "d"},
	}
	o := Combine(ds)
	if o.Effect != AllowWithLimits || o.Policy != "a" || o.Rule != "ra" || o.Message != "a" {
		t.Fatalf("combined %+v", o)
	}
	if *o.Limits != (Limits{TimeoutMs: 300, MaxResponseBytes: 100}) {
		t.Fatalf("merged limits %+v", o.Limits)
	}
	if ds[0].Limits.TimeoutMs != 800 {
		t.Fatal("merging must not change a decision's limits")
	}
	o = Combine(append(ds, Decision{Policy: "e", Effect: RequireApproval, Rule: "re", FailModeApplied: true},
		Decision{Policy: "f", Effect: RequireApproval, Rule: "rf"}))
	if o.Effect != RequireApproval || o.Policy != "e" || !o.FailModeApplied || o.Limits != nil {
		t.Fatalf("the first most restrictive decides: %+v", o)
	}
	o = Combine([]Decision{{Policy: "a", Effect: Allow, Message: "fine"}})
	if o.Effect != Allow || o.Policy != "a" || o.Message != "fine" {
		t.Fatalf("one policy: %+v", o)
	}
	if o := Combine([]Decision{{Effect: Deny, Policy: "x"}, {Effect: Deny, Policy: "y"}}); o.Policy != "x" {
		t.Fatalf("ties: %+v", o)
	}
	single := []Decision{{Policy: "a", Effect: AllowWithLimits, Limits: &Limits{TimeoutMs: 800}},
		{Policy: "b", Effect: Allow, Limits: &Limits{TimeoutMs: 5}}}
	o = Combine(single)
	if o.Limits.TimeoutMs != 800 {
		t.Fatalf("only allow_with_limits decisions bring limits: %+v", o.Limits)
	}
	o.Limits.TimeoutMs = 1
	if single[0].Limits.TimeoutMs != 800 {
		t.Fatal("the combined limits must not share a decision's limits")
	}
	if got := mergeLimits(&Limits{TimeoutMs: 100}, &Limits{MaxResponseBytes: 50}); *got != (Limits{TimeoutMs: 100, MaxResponseBytes: 50}) {
		t.Fatalf("merge fills: %+v", got)
	}
}

// ---------------------------------------------------------------- tests and activation

func TestRunTestsExplainsFailures(t *testing.T) {
	c := mustCompile(t, refundPolicy)
	results := c.RunTests([]Test{
		{Name: "wrong effect", Args: map[string]any{"order_id": "ORD-1", "amount": 150.0}, Expect: Allow},
		{Name: "wrong rule", Args: map[string]any{"order_id": "ORD-1", "amount": 900.0}, Expect: Deny, Rule: "approval-above-100"},
		{Name: "default expected", Args: map[string]any{"order_id": "ORD-1", "amount": 1.0}, Expect: Allow, Rule: "never-above-500"},
	})
	if len(results) != 8 {
		t.Fatalf("results %d", len(results))
	}
	for _, r := range results[:5] {
		if !r.Passed || r.Why != "" {
			t.Fatalf("own test %q failed: %s", r.Name, r.Why)
		}
	}
	if r := results[5]; r.Passed || r.Why != "expected allow, the policy decided require_approval (Refunds above 100 need a person's approval.)" {
		t.Fatalf("effect mismatch: %+v", r)
	}
	if r := results[6]; r.Passed || r.Why != "expected rule approval-above-100 to decide, rule never-above-500 did" {
		t.Fatalf("rule mismatch: %+v", r)
	}
	if r := results[7]; r.Passed || r.Why != "expected rule never-above-500 to decide, the default did" {
		t.Fatalf("default mismatch: %+v", r)
	}
	if r := results[0]; r.Expect != Allow || r.Decision.Effect != Allow || r.Name != "small refund" {
		t.Fatalf("result fields: %+v", r)
	}
	if r := results[1]; r.ExpectRule != "approval-above-100" {
		t.Fatalf("expected rule: %+v", r)
	}
}

func TestTestCasesCarryTheirContext(t *testing.T) {
	c := mustCompile(t, minimal(
		"    - {name: prod-only, when: environment == 'production' && agent == 'a' && agent_version == '1' && subject == 's' && risk == 'READ' && tool == 't' && trace.id == 'x', effect: deny}\n",
		"  tests:\n    - {name: c, expect: deny, context: {environment: production, agent: a, agentVersion: '1', subject: s, risk: READ, traceId: x}}\n"))
	if r := c.RunTests(nil); !r[0].Passed {
		t.Fatalf("context not carried: %+v", r[0])
	}
}

func TestUndecidedRules(t *testing.T) {
	c := mustCompile(t, refundPolicy)
	if u := c.Undecided(); len(u) != 0 {
		t.Fatalf("every rule decides a test: %v", u)
	}
	c = mustCompile(t, minimal("    - {name: a, when: args.n > 1, effect: deny}\n    - {name: b, when: args.n > 2, effect: deny}\n",
		"  tests:\n    - {name: x, args: {n: 5}, expect: deny}\n"))
	if u := c.Undecided(); !reflect.DeepEqual(u, []string{"b"}) {
		t.Fatalf("undecided %v", u)
	}
}

func TestActivationProblems(t *testing.T) {
	if ps := mustCompile(t, refundPolicy).ActivationProblems("WRITE_IRREVERSIBLE"); len(ps) != 0 {
		t.Fatalf("the refund policy may be activated: %v", ps)
	}
	noTests := mustCompile(t, minimal("    - {name: r, when: 'true', effect: deny}\n"))
	if ps := noTests.ActivationProblems(RiskRead); len(ps) != 1 || ps[0].Field != "spec.tests" {
		t.Fatalf("no tests: %v", ps)
	}
	failing := mustCompile(t, minimal("    - {name: r, when: 'true', effect: deny}\n",
		"  tests:\n    - {name: ok, expect: deny}\n    - {name: wrong, expect: allow}\n"))
	ps := failing.ActivationProblems(RiskRead)
	if len(ps) != 1 || ps[0].Field != "spec.tests[1]" || !strings.Contains(ps[0].Message, `"wrong" fails: expected allow`) {
		t.Fatalf("failing test: %v", ps)
	}
	open := mustCompile(t, minimal("    - {name: r, when: 'false', effect: deny}\n",
		"  failMode: fail_open\n  tests:\n    - {name: ok, expect: allow}\n"))
	if ps := open.ActivationProblems(RiskRead); len(ps) != 0 {
		t.Fatalf("fail_open on READ: %v", ps)
	}
	ps = open.ActivationProblems("WRITE_REVERSIBLE")
	if len(ps) != 1 || ps[0].Field != "spec.failMode" || !strings.Contains(ps[0].Message, "t is WRITE_REVERSIBLE") {
		t.Fatalf("fail_open elsewhere: %v", ps)
	}
	if ps := open.ActivationProblems(""); len(ps) != 1 || !strings.Contains(ps[0].Message, "t is not registered") {
		t.Fatalf("fail_open unregistered: %v", ps)
	}
}

// ---------------------------------------------------------------- boundaries

func TestThresholdsAreReadFromTheRules(t *testing.T) {
	c := mustCompile(t, minimal(
		"    - {name: a, when: args.amount > 100, effect: deny}\n"+
			"    - {name: b, when: 100 < args.amount, effect: deny}\n"+
			"    - {name: c, when: double(args.amount) >= 99.5, effect: deny}\n"+
			"    - {name: d, when: \"args['qty'] <= 3u && args.a.b != 7\", effect: deny}\n"+
			"    - {name: e, when: trace.calls == 2, effect: deny}\n"+
			"    - {name: ignored, when: \"has(args.x) && args.x == args.y && args.s == 'k' && trace.id == 'x' && size(args.l) > 3 && int(args.f) + 1 > 3 && args[args.k] > 1 && args.m.all(v, v > 1)\", effect: deny}\n"))
	want := []Threshold{
		{"a", "args.amount", ">", 100, true},
		{"b", "args.amount", ">", 100, true},
		{"c", "args.amount", ">=", 99.5, false},
		{"d", "args.qty", "<=", 3, true},
		{"d", "args.a.b", "!=", 7, true},
		{"e", "trace.calls", "==", 2, true},
	}
	if got := c.Thresholds(); !reflect.DeepEqual(got, want) {
		t.Fatalf("thresholds\n got %+v\nwant %+v", got, want)
	}
	for op, m := range map[string]string{"<=": ">=", ">=": "<=", "<": ">", "==": "==", "!=": "!="} {
		if mirrored[op] != m {
			t.Fatalf("mirror of %s", op)
		}
	}
	c = mustCompile(t, minimal("    - {name: a, when: 5 >= args.n && 5 <= args.m && 5 < args.o && 5 == args.p && 5 != args.q, effect: deny}\n"))
	var ops []string
	for _, th := range c.Thresholds() {
		ops = append(ops, th.Op)
	}
	if !reflect.DeepEqual(ops, []string{"<=", ">=", ">", "==", "!="}) {
		t.Fatalf("mirrored ops %v", ops)
	}
}

func TestBoundariesProbeAroundEveryThreshold(t *testing.T) {
	c := mustCompile(t, refundPolicy)
	base := c.Spec.Spec.Tests[0].Input(c.Spec.Spec.Tool)
	got := c.Boundaries(base)
	type probe struct {
		v    float64
		e    Effect
		rule string
	}
	flat := map[string][]probe{}
	for _, b := range got {
		for _, p := range b.Probes {
			flat[b.Path+"|"+b.Rule] = append(flat[b.Path+"|"+b.Rule], probe{p.Value, p.Effect, p.Rule})
		}
	}
	want := map[string][]probe{
		"trace.calls|one-refund-per-conversation": {{0, Allow, ""}, {1, Deny, "one-refund-per-conversation"}, {2, Deny, "one-refund-per-conversation"}},
		"args.amount|never-above-500": {
			{499, RequireApproval, "approval-above-100"}, {499.99, RequireApproval, "approval-above-100"},
			{500, RequireApproval, "approval-above-100"}, {500.01, Deny, "never-above-500"}, {501, Deny, "never-above-500"}},
		"args.amount|approval-above-100": {
			{99, Allow, ""}, {99.99, Allow, ""}, {100, Allow, ""},
			{100.01, RequireApproval, "approval-above-100"}, {101, RequireApproval, "approval-above-100"}},
	}
	if !reflect.DeepEqual(flat, want) {
		t.Fatalf("boundaries\n got %+v\nwant %+v", flat, want)
	}
	if len(got) != 3 || got[0].Path != "trace.calls" || got[1].Rule != "never-above-500" {
		t.Fatalf("order %+v", got)
	}
	if base.Args["amount"] != 40.0 || base.TraceCalls != 0 {
		t.Fatal("probing must not change the base case")
	}
	dup := mustCompile(t, minimal("    - {name: a, when: args.n > 1, effect: deny}\n    - {name: b, when: args.n > 1, effect: require_approval}\n"))
	if b := dup.Boundaries(Input{}); len(b) != 1 || b[0].Rule != "a" {
		t.Fatalf("one threshold per path and value: %+v", b)
	}
	failing := mustCompile(t, minimal("    - {name: a, when: args.n > 1 && args.other > 0, effect: deny}\n"))
	// false && <error> is false in CEL: below the threshold the missing
	// argument is never read; above it the rule fails and the policy fails closed.
	if b := failing.Boundaries(Input{}); b[0].Probes[0].FailModeApplied || !b[0].Probes[4].FailModeApplied || b[0].Probes[4].Effect != Deny {
		t.Fatalf("a probe records a failed evaluation: %+v", b)
	}
	if _, err := Decode([]byte(`{"kind":"Policy"} {"kind":"Policy"}`)); err == nil || !strings.Contains(err.Error(), "more than one value") {
		t.Fatalf("two JSON values: %v", err)
	}
}

func TestProbeValues(t *testing.T) {
	cases := []struct {
		th   Threshold
		want []float64
	}{
		{Threshold{Path: "args.a", Value: 100, Integer: true}, []float64{99, 99.99, 100, 100.01, 101}},
		{Threshold{Path: "args.a", Value: 99.5}, []float64{99.49, 99.5, 99.51}},
		{Threshold{Path: "trace.calls", Value: 1, Integer: true}, []float64{0, 1, 2}},
		{Threshold{Path: "trace.calls", Value: 0, Integer: true}, []float64{0, 1}},
		{Threshold{Path: "args.a", Value: 0.1}, []float64{0.09, 0.1, 0.11}},
	}
	for _, tc := range cases {
		if got := probeValues(tc.th); !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("%+v: %v, want %v", tc.th, got, tc.want)
		}
	}
}

func TestWithValueCopiesAlongThePath(t *testing.T) {
	inner := map[string]any{"b": 1.0, "keep": "k"}
	base := Input{Args: map[string]any{"a": inner, "x": "y"}, TraceCalls: 4}
	in := withValue(base, "args.a.b", 7)
	if in.Args["a"].(map[string]any)["b"] != 7.0 || in.Args["a"].(map[string]any)["keep"] != "k" || in.Args["x"] != "y" {
		t.Fatalf("set %+v", in.Args)
	}
	if inner["b"] != 1.0 || base.Args["a"].(map[string]any)["b"] != 1.0 {
		t.Fatal("the base must not change")
	}
	if in := withValue(base, "args.new.deep", 2); in.Args["new"].(map[string]any)["deep"] != 2.0 {
		t.Fatalf("missing parents are created: %+v", in.Args)
	}
	if in := withValue(base, "trace.calls", 2); in.TraceCalls != 2 || in.Args["x"] != "y" {
		t.Fatalf("trace.calls: %+v", in)
	}
	if in := withValue(Input{}, "args.a", 3); in.Args["a"] != 3.0 {
		t.Fatalf("empty base: %+v", in)
	}
}

func TestEffectOrder(t *testing.T) {
	order := []Effect{Allow, AllowWithLimits, RequireApproval, Deny}
	for i, a := range order {
		for j, b := range order {
			if MoreRestrictive(a, b) != (i > j) {
				t.Fatalf("%s vs %s", a, b)
			}
		}
		if !a.Valid() {
			t.Fatalf("%s valid", a)
		}
	}
	if Effect("block").Valid() || FailMode("x").Valid() || !FailApproval.Valid() || !FailOpen.Valid() || !FailClosed.Valid() {
		t.Fatal("validity")
	}
}
