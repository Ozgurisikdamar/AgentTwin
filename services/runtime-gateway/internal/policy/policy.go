package policy

import (
	"fmt"
	"strings"
	"sync"

	"cel.dev/cel-go/cel"
)

// CostLimit bounds the evaluation cost of one rule (CEL's cost units). A rule
// that exceeds it fails to evaluate and the policy's fail mode applies.
const CostLimit uint64 = 100_000

// Variables are the names a rule can read, in the order they are documented.
var Variables = []string{"tool", "args", "risk", "agent", "agent_version", "environment", "subject", "trace"}

var celEnv = sync.OnceValues(func() (*cel.Env, error) {
	return cel.NewEnv(
		cel.Variable("tool", cel.StringType),
		cel.Variable("args", cel.MapType(cel.StringType, cel.DynType)),
		cel.Variable("risk", cel.StringType),
		cel.Variable("agent", cel.StringType),
		cel.Variable("agent_version", cel.StringType),
		cel.Variable("environment", cel.StringType),
		cel.Variable("subject", cel.StringType),
		cel.Variable("trace", cel.MapType(cel.StringType, cel.DynType)),
		// JSON numbers are doubles: `args.amount > 100` compares a double
		// with an int. (The default of this cel-go version; stated so a
		// dependency upgrade cannot change it.)
		cel.CrossTypeNumericComparisons(true),
	)
})

// Input is the action a policy decides.
type Input struct {
	Tool         string
	Risk         string
	Agent        string
	AgentVersion string
	Environment  string
	Subject      string
	TraceID      string
	// TraceCalls is how many times the tool was forwarded in the same trace
	// before this call.
	TraceCalls int
	// Args are the tool's arguments as decoded from JSON (numbers are float64).
	Args map[string]any
}

func (in Input) activation() map[string]any {
	// A nil Args reads as an empty map in CEL.
	return map[string]any{
		"tool": in.Tool, "args": in.Args, "risk": in.Risk, "agent": in.Agent,
		"agent_version": in.AgentVersion, "environment": in.Environment, "subject": in.Subject,
		"trace": map[string]any{"id": in.TraceID, "calls": int64(in.TraceCalls)},
	}
}

// Compiled is a policy ready to decide.
type Compiled struct {
	Spec  Spec
	Hash  string
	rules []compiledRule
}

type compiledRule struct {
	Rule
	program    cel.Program
	thresholds []Threshold
}

// Compile type-checks every rule of a valid document against the declared
// variables. A rule must be a boolean expression.
func Compile(s Spec) (*Compiled, error) {
	if ps := s.problems(); len(ps) > 0 {
		return nil, &InvalidError{ps}
	}
	env, err := celEnv()
	if err != nil {
		return nil, fmt.Errorf("policy: CEL environment: %w", err)
	}
	c := &Compiled{Spec: s, Hash: s.Hash()}
	var ps []Problem
	for i, r := range s.Spec.Rules {
		field := fmt.Sprintf("spec.rules[%d].when", i)
		ast, iss := env.Compile(r.When)
		if iss != nil && iss.Err() != nil {
			ps = append(ps, Problem{field, strings.ReplaceAll(iss.Err().Error(), "<input>", r.Name)})
			continue
		}
		if !ast.OutputType().IsExactType(cel.BoolType) {
			ps = append(ps, Problem{field, fmt.Sprintf("must be a boolean expression, not %s", ast.OutputType())})
			continue
		}
		prg, err := env.Program(ast, cel.CostLimit(CostLimit), cel.EvalOptions(cel.OptOptimize))
		if err != nil {
			ps = append(ps, Problem{field, err.Error()})
			continue
		}
		c.rules = append(c.rules, compiledRule{Rule: r, program: prg, thresholds: thresholdsOf(r.Name, ast)})
	}
	if len(ps) > 0 {
		return nil, &InvalidError{ps}
	}
	return c, nil
}

// Match is a rule whose condition held.
type Match struct {
	Rule    string `json:"rule"`
	Effect  Effect `json:"effect"`
	Message string `json:"message,omitempty"`
}

// RuleError is a rule that could not be evaluated.
type RuleError struct {
	Rule  string `json:"rule"`
	Error string `json:"error"`
}

// Decision is what one policy decides for one action.
type Decision struct {
	Policy string `json:"policy"`
	Effect Effect `json:"effect"`
	// Rule decided; empty when no rule matched and the default applied.
	Rule    string `json:"rule,omitempty"`
	Message string `json:"message"`
	// Matched lists every rule whose condition held, in order.
	Matched []Match     `json:"matched"`
	Errors  []RuleError `json:"errors,omitempty"`
	// FailModeApplied is set when a rule failed to evaluate.
	FailModeApplied bool    `json:"fail_mode_applied"`
	Limits          *Limits `json:"limits,omitempty"`
}

// Decide evaluates every rule. A rule that matched deny denies, whatever
// else happened. Otherwise a rule that failed applies the fail mode
// (fail_open only for a READ tool; elsewhere it fails closed). Otherwise the
// most restrictive matching effect wins, reported by the first rule with it;
// no match gives the default.
func (c *Compiled) Decide(in Input) Decision {
	act := in.activation()
	d := Decision{Policy: c.Spec.Metadata.Name, Matched: []Match{}}
	for _, r := range c.rules {
		out, _, err := r.program.Eval(act)
		if err != nil {
			d.Errors = append(d.Errors, RuleError{r.Name, err.Error()})
			continue
		}
		matched, ok := out.Value().(bool)
		if !ok {
			d.Errors = append(d.Errors, RuleError{r.Name, fmt.Sprintf("returned %v, not a boolean", out.Type())})
			continue
		}
		if matched {
			d.Matched = append(d.Matched, Match{r.Name, r.Effect, r.Message})
		}
	}
	decide := func(m Match) {
		d.Effect, d.Rule, d.Message = m.Effect, m.Rule, m.Message
		if d.Message == "" {
			d.Message = fmt.Sprintf("Rule %s: %s.", m.Rule, m.Effect)
		}
	}
	if m, ok := first(d.Matched, Deny); ok {
		decide(m)
		return d.withLimits(c.Spec)
	}
	if len(d.Errors) > 0 {
		d.FailModeApplied = true
		e := d.Errors[0]
		mode := c.Spec.Spec.FailMode
		switch {
		case mode == FailApproval:
			d.Effect, d.Rule = RequireApproval, e.Rule
			d.Message = fmt.Sprintf("Rule %s could not be evaluated (%s); the policy asks for approval.", e.Rule, e.Error)
			return d
		case mode == FailOpen && in.Risk == RiskRead:
			// A read-only tool proceeds with what the other rules decided.
		case mode == FailOpen:
			d.Effect, d.Rule = Deny, e.Rule
			d.Message = fmt.Sprintf("Rule %s could not be evaluated (%s); fail_open applies only to READ tools, so the policy fails closed.", e.Rule, e.Error)
			return d
		default:
			d.Effect, d.Rule = Deny, e.Rule
			d.Message = fmt.Sprintf("Rule %s could not be evaluated (%s); the policy fails closed.", e.Rule, e.Error)
			return d
		}
	}
	best := Match{Effect: c.Spec.Spec.Default}
	found := false
	for _, m := range d.Matched {
		if !found || MoreRestrictive(m.Effect, best.Effect) {
			best, found = m, true
		}
	}
	if !found {
		d.Effect = c.Spec.Spec.Default
		d.Message = fmt.Sprintf("No rule matched; the policy's default is %s.", d.Effect)
		return d.withLimits(c.Spec)
	}
	decide(best)
	return d.withLimits(c.Spec)
}

func (d Decision) withLimits(s Spec) Decision {
	if d.Effect == AllowWithLimits && s.Spec.Limits != nil {
		l := *s.Spec.Limits
		d.Limits = &l
	}
	return d
}

func first(ms []Match, e Effect) (Match, bool) {
	for _, m := range ms {
		if m.Effect == e {
			return m, true
		}
	}
	return Match{}, false
}

// Outcome is the decision of every active policy of a tool, combined.
type Outcome struct {
	Effect Effect `json:"effect"`
	// Policy and Rule decided; both empty when no policy applies.
	Policy    string     `json:"policy,omitempty"`
	Rule      string     `json:"rule,omitempty"`
	Message   string     `json:"message"`
	Decisions []Decision `json:"decisions"`
	Limits    *Limits    `json:"limits,omitempty"`
	// FailModeApplied is set when the deciding policy applied its fail mode.
	FailModeApplied bool `json:"fail_mode_applied"`
}

// NoPolicy is the message of an action no active policy guards.
const NoPolicy = "No active policy guards this tool."

// Combine combines the decisions of a tool's active policies, in the
// caller's order: the most restrictive effect wins, the first policy with it
// is the one reported. Limits of every policy that allowed with limits are
// merged (the smallest of each). No decision allows.
func Combine(ds []Decision) Outcome {
	o := Outcome{Effect: Allow, Message: NoPolicy, Decisions: ds}
	if o.Decisions == nil {
		o.Decisions = []Decision{}
	}
	for i, d := range ds {
		if i == 0 || MoreRestrictive(d.Effect, o.Effect) {
			o.Effect, o.Policy, o.Rule, o.Message, o.FailModeApplied = d.Effect, d.Policy, d.Rule, d.Message, d.FailModeApplied
		}
	}
	if o.Effect == AllowWithLimits {
		for _, d := range ds {
			if d.Effect == AllowWithLimits && d.Limits != nil {
				o.Limits = mergeLimits(o.Limits, d.Limits)
			}
		}
	}
	return o
}

func mergeLimits(a, b *Limits) *Limits {
	if a == nil {
		l := *b
		return &l
	}
	out := *a
	if b.TimeoutMs > 0 && (out.TimeoutMs == 0 || b.TimeoutMs < out.TimeoutMs) {
		out.TimeoutMs = b.TimeoutMs
	}
	if b.MaxResponseBytes > 0 && (out.MaxResponseBytes == 0 || b.MaxResponseBytes < out.MaxResponseBytes) {
		out.MaxResponseBytes = b.MaxResponseBytes
	}
	return &out
}
