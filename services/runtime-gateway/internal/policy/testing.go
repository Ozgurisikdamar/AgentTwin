package policy

import (
	"fmt"
	"maps"
	"math"
	"slices"
	"strings"

	"cel.dev/cel-go/cel"
	celast "cel.dev/cel-go/common/ast"
	"cel.dev/cel-go/common/operators"
	"cel.dev/cel-go/common/types"
)

// ---------------------------------------------------------------- tests

// TestResult is a test case and what the policy decided for it.
type TestResult struct {
	Name       string   `json:"name"`
	Expect     Effect   `json:"expect"`
	ExpectRule string   `json:"expect_rule,omitempty"`
	Decision   Decision `json:"decision"`
	Passed     bool     `json:"passed"`
	// Why explains a failure.
	Why string `json:"why,omitempty"`
}

// Input is the action a test case describes.
func (t Test) Input(tool string) Input {
	return Input{
		Tool: tool, Risk: t.Context.Risk, Agent: t.Context.Agent, AgentVersion: t.Context.AgentVersion,
		Environment: t.Context.Environment, Subject: t.Context.Subject, TraceID: t.Context.TraceID,
		TraceCalls: t.Context.TraceCalls, Args: t.Args,
	}
}

// RunTests decides the policy's own tests and then the extra cases.
func (c *Compiled) RunTests(extra []Test) []TestResult {
	cases := slices.Concat(c.Spec.Spec.Tests, extra)
	out := make([]TestResult, 0, len(cases))
	for _, t := range cases {
		d := c.Decide(t.Input(c.Spec.Spec.Tool))
		r := TestResult{Name: t.Name, Expect: t.Expect, ExpectRule: t.Rule, Decision: d, Passed: true}
		switch {
		case d.Effect != t.Expect:
			r.Passed = false
			r.Why = fmt.Sprintf("expected %s, the policy decided %s (%s)", t.Expect, d.Effect, d.Message)
		case t.Rule != "" && d.Rule != t.Rule:
			r.Passed = false
			r.Why = fmt.Sprintf("expected rule %s to decide, %s did", t.Rule, orDefault(d.Rule))
		}
		out = append(out, r)
	}
	return out
}

func orDefault(rule string) string {
	if rule == "" {
		return "the default"
	}
	return "rule " + rule
}

// Undecided lists the rules that decide none of the policy's own tests.
func (c *Compiled) Undecided() []string {
	decided := map[string]bool{}
	for _, r := range c.RunTests(nil) {
		decided[r.Decision.Rule] = true
	}
	var out []string
	for _, r := range c.rules {
		if !decided[r.Name] {
			out = append(out, r.Name)
		}
	}
	return out
}

// ActivationProblems lists why the policy may not be activated for a tool of
// the given risk tier (empty when the tool is not registered): it needs
// tests, every test must pass, and only a READ tool may fail open.
func (c *Compiled) ActivationProblems(toolRisk string) []Problem {
	var ps []Problem
	if len(c.Spec.Spec.Tests) == 0 {
		ps = append(ps, Problem{"spec.tests", "a policy needs at least one test before it can be activated"})
	}
	for i, r := range c.RunTests(nil) {
		if !r.Passed {
			ps = append(ps, Problem{fmt.Sprintf("spec.tests[%d]", i), fmt.Sprintf("%q fails: %s", r.Name, r.Why)})
		}
	}
	if c.Spec.Spec.FailMode == FailOpen && toolRisk != RiskRead {
		risk := toolRisk
		if risk == "" {
			risk = "not registered"
		}
		ps = append(ps, Problem{"spec.failMode", fmt.Sprintf("fail_open is allowed only for a READ tool; %s is %s", c.Spec.Spec.Tool, risk)})
	}
	return ps
}

// ---------------------------------------------------------------- boundaries

// Threshold is a numeric comparison a rule makes against a constant.
type Threshold struct {
	Rule string `json:"rule"`
	// Path is what is compared: args.<name>[.<name>...] or trace.calls.
	Path    string  `json:"path"`
	Op      string  `json:"op"`
	Value   float64 `json:"value"`
	Integer bool    `json:"integer"`
}

// Probe is the decision at one value around a threshold.
type Probe struct {
	Value           float64 `json:"value"`
	Effect          Effect  `json:"effect"`
	Rule            string  `json:"rule,omitempty"`
	FailModeApplied bool    `json:"fail_mode_applied"`
}

// Boundary is a threshold and the decisions around it.
type Boundary struct {
	Threshold
	Probes []Probe `json:"probes"`
}

// Thresholds lists the numeric comparisons of every rule, in rule order.
func (c *Compiled) Thresholds() []Threshold {
	var out []Threshold
	for _, r := range c.rules {
		out = append(out, r.thresholds...)
	}
	return out
}

// Boundaries decides the action at values around every threshold, starting
// from base: for an argument compared with an integer, one unit and one
// hundredth either side and the value itself; with a decimal, one hundredth
// either side; for trace.calls, one call either side (never negative).
func (c *Compiled) Boundaries(base Input) []Boundary {
	seen := map[string]bool{}
	var out []Boundary
	for _, t := range c.Thresholds() {
		key := fmt.Sprintf("%s|%g", t.Path, t.Value)
		if seen[key] {
			continue
		}
		seen[key] = true
		b := Boundary{Threshold: t}
		for _, v := range probeValues(t) {
			d := c.Decide(withValue(base, t.Path, v))
			b.Probes = append(b.Probes, Probe{Value: v, Effect: d.Effect, Rule: d.Rule, FailModeApplied: d.FailModeApplied})
		}
		out = append(out, b)
	}
	return out
}

func probeValues(t Threshold) []float64 {
	var vs []float64
	switch {
	case t.Path == "trace.calls":
		vs = []float64{t.Value - 1, t.Value, t.Value + 1}
	case t.Integer:
		vs = []float64{t.Value - 1, t.Value - 0.01, t.Value, t.Value + 0.01, t.Value + 1}
	default:
		vs = []float64{t.Value - 0.01, t.Value, t.Value + 0.01}
	}
	out := vs[:0]
	for _, v := range vs {
		v = math.Round(v*100) / 100
		if t.Path == "trace.calls" && v < 0 {
			continue
		}
		out = append(out, v)
	}
	return out
}

// withValue returns base with the value at path set (maps along the path are
// copied, never shared with base).
func withValue(base Input, path string, v float64) Input {
	in := base
	if path == "trace.calls" {
		in.TraceCalls = int(v)
		return in
	}
	parts := strings.Split(strings.TrimPrefix(path, "args."), ".")
	in.Args = setPath(base.Args, parts, v)
	return in
}

func setPath(m map[string]any, parts []string, v float64) map[string]any {
	out := make(map[string]any, len(m)+1)
	maps.Copy(out, m)
	if len(parts) == 1 {
		out[parts[0]] = v
		return out
	}
	child, _ := m[parts[0]].(map[string]any)
	out[parts[0]] = setPath(child, parts[1:], v)
	return out
}

var comparisons = map[string]string{
	operators.Greater: ">", operators.GreaterEquals: ">=", operators.Less: "<",
	operators.LessEquals: "<=", operators.Equals: "==", operators.NotEquals: "!=",
}

// mirrored is the operator seen from the other side (100 < x is x > 100).
var mirrored = map[string]string{">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "==", "!=": "!="}

func thresholdsOf(rule string, ast *cel.Ast) []Threshold {
	var out []Threshold
	celast.PreOrderVisit(ast.NativeRep().Expr(), celast.NewExprVisitor(func(e celast.Expr) {
		if e.Kind() != celast.CallKind {
			return
		}
		call := e.AsCall()
		op, ok := comparisons[call.FunctionName()]
		if !ok || len(call.Args()) != 2 {
			return
		}
		l, r := call.Args()[0], call.Args()[1]
		if path, ok := pathOf(l); ok {
			if v, isInt, ok := numberOf(r); ok {
				out = append(out, Threshold{rule, path, op, v, isInt})
			}
		} else if path, ok := pathOf(r); ok {
			if v, isInt, ok := numberOf(l); ok {
				out = append(out, Threshold{rule, path, mirrored[op], v, isInt})
			}
		}
	}))
	return out
}

// pathOf reads args.a.b, args['a'] or trace.calls, possibly wrapped in a
// numeric conversion (double(args.amount)).
func pathOf(e celast.Expr) (string, bool) {
	if e.Kind() == celast.CallKind {
		call := e.AsCall()
		switch call.FunctionName() {
		case "double", "int", "uint":
			if len(call.Args()) == 1 && !call.IsMemberFunction() {
				return pathOf(call.Args()[0])
			}
		}
	}
	var parts []string
	for {
		switch e.Kind() {
		case celast.SelectKind:
			s := e.AsSelect()
			if s.IsTestOnly() {
				return "", false
			}
			parts = append(parts, s.FieldName())
			e = s.Operand()
			continue
		case celast.CallKind:
			call := e.AsCall()
			if call.FunctionName() != operators.Index || len(call.Args()) != 2 {
				return "", false
			}
			// AsLiteral is nil for anything but a literal.
			s, ok := call.Args()[1].AsLiteral().(types.String)
			if !ok {
				return "", false
			}
			parts = append(parts, string(s))
			e = call.Args()[0]
			continue
		case celast.IdentKind:
			root := e.AsIdent()
			slices.Reverse(parts)
			switch {
			case root == "args" && len(parts) > 0:
				return "args." + strings.Join(parts, "."), true
			case root == "trace" && len(parts) == 1 && parts[0] == "calls":
				return "trace.calls", true
			}
			return "", false
		default:
			return "", false
		}
	}
}

func numberOf(e celast.Expr) (float64, bool, bool) {
	if e.Kind() != celast.LiteralKind {
		return 0, false, false
	}
	switch v := e.AsLiteral().(type) {
	case types.Int:
		return float64(v), true, true
	case types.Uint:
		return float64(v), true, true
	case types.Double:
		return float64(v), false, true
	}
	return 0, false, false
}
