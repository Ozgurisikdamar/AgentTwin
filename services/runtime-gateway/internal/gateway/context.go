package gateway

import (
	"net/http"
	"regexp"
	"strings"
)

// Request headers of an invocation (ADR-0033).
const (
	HeaderProject        = "X-AgentTwin-Project"
	HeaderAgent          = "X-AgentTwin-Agent"
	HeaderAgentVersion   = "X-AgentTwin-Agent-Version"
	HeaderEnvironment    = "X-AgentTwin-Environment"
	HeaderTraceID        = "X-AgentTwin-Trace-Id"
	HeaderTraceparent    = "Traceparent"
	HeaderIdempotencyKey = "Idempotency-Key"
	HeaderApprovalToken  = "X-AgentTwin-Approval-Token" //nolint:gosec // a header name, not a credential
	// ArgIdempotencyKey is the argument an agent may carry its key in.
	ArgIdempotencyKey = "idempotency_key"
	// DefaultEnvironment is the environment of a call that names none.
	DefaultEnvironment = "production"
)

// Response headers of a decided invocation.
const (
	HeaderDecision      = "X-AgentTwin-Decision"
	HeaderDecisionID    = "X-AgentTwin-Decision-Id"
	HeaderPolicy        = "X-AgentTwin-Policy"
	HeaderPolicyVersion = "X-AgentTwin-Policy-Version"
	HeaderPolicyRule    = "X-AgentTwin-Policy-Rule"
	HeaderReplayed      = "Idempotent-Replayed"
)

var (
	namePattern        = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$`)
	environmentPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,39}$`)
	traceIDPattern     = regexp.MustCompile(`^[0-9a-f]{32}$`)
	traceparentPattern = regexp.MustCompile(`^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$`)
	keyPattern         = regexp.MustCompile(`^[\x21-\x7e]{1,200}$`)
	uuidPattern        = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// Context is an invocation's context, from its headers and arguments.
type Context struct {
	Project        string
	Agent          string
	AgentVersion   string
	Environment    string
	TraceID        string
	IdempotencyKey string
	ApprovalToken  string
}

// Problem is a header that cannot be used, and why.
type Problem struct {
	Field   string `json:"field"`
	Message string `json:"message"`
}

// ReadContext reads the context of a call. The trace comes from traceparent
// or X-AgentTwin-Trace-Id (they must agree when both are sent); the
// idempotency key from Idempotency-Key or the idempotency_key argument
// (likewise).
func ReadContext(h http.Header, args Args) (Context, []Problem) {
	c := Context{
		Project:       strings.TrimSpace(h.Get(HeaderProject)),
		Agent:         strings.TrimSpace(h.Get(HeaderAgent)),
		AgentVersion:  strings.TrimSpace(h.Get(HeaderAgentVersion)),
		Environment:   strings.TrimSpace(h.Get(HeaderEnvironment)),
		ApprovalToken: strings.TrimSpace(h.Get(HeaderApprovalToken)),
	}
	var ps []Problem
	add := func(field, msg string) { ps = append(ps, Problem{field, msg}) }
	if c.Project != "" && !uuidPattern.MatchString(c.Project) {
		add(HeaderProject, "must be a project id")
	}
	if c.Agent != "" && !namePattern.MatchString(c.Agent) {
		add(HeaderAgent, "must be a name of up to 200 characters")
	}
	if c.AgentVersion != "" && !namePattern.MatchString(c.AgentVersion) {
		add(HeaderAgentVersion, "must be a version of up to 200 characters")
	}
	if c.Environment == "" {
		c.Environment = DefaultEnvironment
	} else if !environmentPattern.MatchString(c.Environment) {
		add(HeaderEnvironment, "must be lowercase letters, digits, _ and - (up to 40)")
	}
	if c.ApprovalToken != "" && !TokenPattern.MatchString(c.ApprovalToken) {
		add(HeaderApprovalToken, "is not an approval token")
	}

	explicit := strings.ToLower(strings.TrimSpace(h.Get(HeaderTraceID)))
	var fromParent string
	if tp := strings.ToLower(strings.TrimSpace(h.Get(HeaderTraceparent))); tp != "" {
		if m := traceparentPattern.FindStringSubmatch(tp); m != nil && !allZero(m[1]) {
			fromParent = m[1]
		} else {
			add(HeaderTraceparent, "is not a W3C traceparent")
		}
	}
	switch {
	case explicit != "" && (!traceIDPattern.MatchString(explicit) || allZero(explicit)):
		add(HeaderTraceID, "must be 32 lowercase hex characters, not all zero")
	case explicit != "" && fromParent != "" && explicit != fromParent:
		add(HeaderTraceID, "names another trace than traceparent")
	case explicit != "":
		c.TraceID = explicit
	default:
		c.TraceID = fromParent
	}

	header := strings.TrimSpace(h.Get(HeaderIdempotencyKey))
	arg, hasArg := args.Values[ArgIdempotencyKey]
	argKey, isString := arg.(string)
	switch {
	case hasArg && arg != nil && !isString:
		add(ArgIdempotencyKey, "must be a string")
	case header != "" && !keyPattern.MatchString(header):
		add(HeaderIdempotencyKey, "must be 1 to 200 printable ASCII characters")
	case isString && argKey != "" && !keyPattern.MatchString(argKey):
		add(ArgIdempotencyKey, "must be 1 to 200 printable ASCII characters")
	case header != "" && isString && argKey != "" && header != argKey:
		add(HeaderIdempotencyKey, "differs from the idempotency_key argument")
	case header != "":
		c.IdempotencyKey = header
	default:
		c.IdempotencyKey = argKey
	}
	return c, ps
}

func allZero(s string) bool { return strings.Trim(s, "0") == "" }
