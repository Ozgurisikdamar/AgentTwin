package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/gateway"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/policy"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

// GatewayRoutes registers the invocation path.
func (s *Server) GatewayRoutes(mux Router) {
	h := httpx.Handle
	mux.HandleFunc("POST /gateway/v1/tools/{tool}", h(s.invoke))
	mux.HandleFunc("GET /gateway/v1/approvals/{approval_id}", h(s.gatewayApproval))
	mux.HandleFunc("POST /gateway/v1/approvals/{approval_id}/token", h(s.claimToken))
}

// recordedDecision is one policy's decision as recorded with the decision.
type recordedDecision struct {
	policy.Decision
	PolicyVersionID string `json:"policy_version_id"`
	Version         int    `json:"version"`
}

// invocation is a call as read from its request.
type invocation struct {
	p           authn.Principal
	sc          store.Scope
	tool        string
	endpoint    store.Endpoint
	ctx         gateway.Context
	args        gateway.Args
	hash        string
	redacted    map[string]any
	summary     string
	traceparent string
	// headers are the request headers the endpoint forwards.
	headers http.Header
}

// verdict is what the decision transaction concluded.
type verdict struct {
	decision store.Decision
	version  int // the deciding policy's version number
	// refusal is the error answered (after the transaction commits).
	refusal error
	replay  *store.Idem
	forward bool
	limits  *policy.Limits
}

// gatewayProject is the project a gateway call acts in: the header, or the
// caller's only project.
func gatewayProject(r *http.Request, named string) (authn.Principal, store.Scope, error) {
	p, err := authn.Require(r, authn.PermRuntimeInvoke)
	if err != nil {
		return p, store.Scope{}, err
	}
	project := strings.ToLower(named)
	if project == "" {
		if len(p.ProjectIDs) != 1 {
			return p, store.Scope{}, httpx.Invalid("PROJECT_REQUIRED",
				"Name the project in the "+gateway.HeaderProject+" header (the key reaches more than one).",
				map[string]any{"field": gateway.HeaderProject})
		}
		project = strings.ToLower(p.ProjectIDs[0])
	}
	if !ids.Valid(project) {
		return p, store.Scope{}, invalidField(gateway.HeaderProject, gateway.HeaderProject+" must be a project id.")
	}
	p, err = authn.RequireProject(r, authn.PermRuntimeInvoke, project)
	if err != nil {
		return p, store.Scope{}, err
	}
	return p, store.Scope{OrgID: p.OrgID, ProjectID: project}, nil
}

func (s *Server) readInvocation(w http.ResponseWriter, r *http.Request) (invocation, error) {
	tool, err := pathTool(r)
	if err != nil {
		return invocation{}, err
	}
	if _, err := authn.Require(r, authn.PermRuntimeInvoke); err != nil {
		return invocation{}, err
	}
	body, err := httpx.ReadLimited(w, r, gateway.MaxArgsBytes)
	if err != nil {
		return invocation{}, err
	}
	args, err := gateway.ParseArgs(body)
	if err != nil {
		return invocation{}, httpx.Invalid("INVALID_ARGUMENTS", "The body must be the tool's arguments as one JSON object: "+
			strings.TrimPrefix(err.Error(), gateway.ErrArgs.Error()+": ")+".", nil)
	}
	c, problems := gateway.ReadContext(r.Header, args)
	if len(problems) > 0 {
		return invocation{}, httpx.Invalid("INVALID_CONTEXT", problems[0].Field+" "+problems[0].Message+".",
			map[string]any{"problems": problems})
	}
	p, sc, err := gatewayProject(r, c.Project)
	if err != nil {
		return invocation{}, err
	}
	e, err := s.Store.Endpoint(r.Context(), s.Store.Pool, sc, tool)
	if errors.Is(err, store.ErrNotFound) {
		return invocation{}, httpx.NewError(http.StatusNotFound, "TOOL_NOT_REGISTERED",
			fmt.Sprintf("%s is not registered with the gateway; register it with PUT /api/v1/tool-endpoints/%s.", tool, tool)).
			WithDetails(map[string]any{"tool": tool})
	}
	if err != nil {
		return invocation{}, unavailable(err)
	}
	if e.Idempotency == "required" && c.IdempotencyKey == "" {
		return invocation{}, httpx.Invalid("IDEMPOTENCY_KEY_REQUIRED",
			fmt.Sprintf("%s is %s: send an Idempotency-Key header (or an idempotency_key argument) so a retry cannot repeat it.", tool, e.Risk),
			map[string]any{"field": gateway.HeaderIdempotencyKey})
	}
	redacted, _ := gateway.RedactArgs(args.Values).(map[string]any)
	inv := invocation{p: p, sc: sc, tool: tool, endpoint: e, ctx: c, args: args, redacted: redacted,
		summary: gateway.Describe(tool, redacted), traceparent: strings.TrimSpace(r.Header.Get(gateway.HeaderTraceparent)),
		headers: forwardable(e, r.Header)}
	inv.hash = gateway.Action{Organization: sc.OrgID, Project: sc.ProjectID, Agent: c.Agent, Tool: tool, Args: args}.Hash()
	return inv, nil
}

// unavailable is the answer when the gateway cannot decide: nothing is
// forwarded.
func unavailable(err error) error {
	var api *httpx.Error
	if errors.As(err, &api) {
		return err
	}
	if errors.Is(err, context.Canceled) {
		return err
	}
	return httpx.NewError(http.StatusServiceUnavailable, "GATEWAY_UNAVAILABLE",
		"The gateway could not decide this call; nothing was forwarded. Retry shortly.")
}

func (s *Server) invoke(w http.ResponseWriter, r *http.Request) error {
	inv, err := s.readInvocation(w, r)
	if err != nil {
		return err
	}
	// The decision is recorded and the call completed even if the agent
	// hangs up: its outcome must not be lost.
	ctx := context.WithoutCancel(r.Context())
	var v verdict
	err = pgx.BeginFunc(ctx, s.Store.Pool, func(tx pgx.Tx) error {
		v = verdict{}
		return s.decideCall(ctx, tx, inv, &v)
	})
	if err != nil {
		s.Log.ErrorContext(ctx, "gateway decision failed", "tool", inv.tool, "error", err.Error())
		return unavailable(err)
	}
	setDecisionHeaders(w.Header(), v.decision, v.version)
	switch {
	case v.refusal != nil:
		return v.refusal
	case v.replay != nil:
		w.Header().Set(gateway.HeaderReplayed, "true")
		if orig, err := s.Store.Decision(ctx, inv.sc, v.replay.DecisionID); err == nil && orig.Effect != nil {
			w.Header().Set(gateway.HeaderDecision, *orig.Effect)
		}
		status := http.StatusOK
		if v.replay.ResponseStatus != nil {
			status = *v.replay.ResponseStatus
		}
		ct := "application/json"
		if v.replay.ResponseContentType != nil {
			ct = *v.replay.ResponseContentType
		}
		w.Header().Set("Content-Type", ct)
		w.WriteHeader(status)
		_, _ = w.Write(v.replay.ResponseBody)
		return nil
	}
	return s.complete(ctx, w, inv, v)
}

// decideCall runs in one transaction: idempotency, the policies, the
// approval, and the record of the decision. Calls on the same key, trace and
// action are serialized (locks in that order).
func (s *Server) decideCall(ctx context.Context, tx pgx.Tx, inv invocation, v *verdict) error {
	c, sc := inv.ctx, inv.sc
	var locks []string
	if c.IdempotencyKey != "" {
		locks = append(locks, "idem:"+sc.OrgID+":"+sc.ProjectID+":"+inv.tool+":"+c.IdempotencyKey)
	}
	if c.TraceID != "" {
		locks = append(locks, "trace:"+sc.OrgID+":"+sc.ProjectID+":"+c.TraceID+":"+inv.tool)
	}
	locks = append(locks, "action:"+inv.hash)
	if err := s.Store.Lock(ctx, tx, locks...); err != nil {
		return err
	}
	now := s.now()
	d := store.Decision{ID: ids.New(), Tool: inv.tool, Risk: inv.endpoint.Risk, Agent: c.Agent,
		AgentVersion: c.AgentVersion, Environment: c.Environment, Subject: inv.p.Actor, TraceID: optional(c.TraceID),
		ActionHash: inv.hash, Arguments: inv.redacted, Summary: inv.summary, IdempotencyKey: optional(c.IdempotencyKey),
		CreatedAt: now}

	if c.IdempotencyKey != "" {
		rec, err := s.Store.LockIdem(ctx, tx, sc, inv.tool, c.IdempotencyKey, now)
		if err != nil {
			return err
		}
		var core *gateway.IdemRecord
		if rec != nil {
			core = &gateway.IdemRecord{ActionHash: rec.ActionHash, State: rec.State, UpdatedAt: rec.UpdatedAt}
		}
		switch gateway.CheckIdempotency(core, inv.hash, now) {
		case gateway.IdemReused:
			v.refusal = httpx.NewError(http.StatusConflict, "IDEMPOTENCY_KEY_REUSED",
				"This idempotency key was used for another action; use a new key for a new action.")
			return nil
		case gateway.IdemBusy:
			v.refusal = httpx.NewError(http.StatusConflict, "IDEMPOTENCY_IN_PROGRESS",
				"A call with this idempotency key is still in progress; retry after it completes.")
			return nil
		case gateway.IdemReplay:
			d.Outcome = store.OutcomeReplayed
			d.Message = "Replayed the response of decision " + rec.DecisionID + "."
			v.decision, v.replay = d, rec
			return s.Store.InsertDecision(ctx, tx, sc, d)
		}
	}

	actives, err := s.Store.ActiveVersions(ctx, tx, sc, inv.tool)
	if err != nil {
		return err
	}
	calls, err := s.Store.TraceCalls(ctx, tx, sc, c.TraceID, inv.tool, inv.hash)
	if err != nil {
		return err
	}
	in := policy.Input{Tool: inv.tool, Risk: inv.endpoint.Risk, Agent: c.Agent, AgentVersion: c.AgentVersion,
		Environment: c.Environment, Subject: inv.p.Actor, TraceID: c.TraceID, TraceCalls: calls, Args: inv.args.Values}
	decisions := make([]policy.Decision, 0, len(actives))
	recorded := make([]recordedDecision, 0, len(actives))
	byName := map[string]store.ActiveVersion{}
	compiled := map[string]*policy.Compiled{}
	for _, a := range actives {
		cp, err := s.policies.compiled(a.VersionID, a.Spec)
		if err != nil {
			return err
		}
		pd := cp.Decide(in)
		decisions = append(decisions, pd)
		recorded = append(recorded, recordedDecision{Decision: pd, PolicyVersionID: a.VersionID, Version: a.Version})
		byName[a.PolicyName], compiled[a.PolicyName] = a, cp
	}
	out := policy.Combine(decisions)
	effect := string(out.Effect)
	d.Effect, d.Message, d.FailModeApplied = &effect, out.Message, out.FailModeApplied
	d.Decisions, _ = json.Marshal(recorded)
	if out.Policy != "" {
		deciding := byName[out.Policy]
		d.PolicyName, d.PolicyVersionID = &out.Policy, &deciding.VersionID
		v.version = deciding.Version
	}
	d.Rule = optional(out.Rule)
	if out.Limits != nil {
		d.Limits, _ = json.Marshal(out.Limits)
		v.limits = out.Limits
	}

	switch out.Effect {
	case policy.Deny:
		d.Outcome = store.OutcomeDenied
		v.decision = d
		v.refusal = httpx.NewError(http.StatusForbidden, "POLICY_DENIED", out.Message).
			WithDetails(map[string]any{"decision_id": d.ID, "policy": out.Policy, "rule": out.Rule})
		if err := s.Store.InsertDecision(ctx, tx, sc, d); err != nil {
			return err
		}
		return s.violation(ctx, tx, inv, d, out.Message)
	case policy.RequireApproval:
		if c.ApprovalToken != "" {
			return s.useToken(ctx, tx, inv, d, v, now)
		}
		return s.askApproval(ctx, tx, inv, d, v, compiled[out.Policy], now)
	default:
		return s.admit(ctx, tx, inv, d, v, nil, now)
	}
}

func optional(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// admit records a call that is forwarded: its key claimed, the trace's call
// counted, the approval (if any) used.
func (s *Server) admit(ctx context.Context, tx pgx.Tx, inv invocation, d store.Decision, v *verdict,
	approval *store.Approval, now time.Time) error {
	sc, c := inv.sc, inv.ctx
	if c.IdempotencyKey != "" {
		if err := s.Store.ClaimIdem(ctx, tx, sc, inv.tool, c.IdempotencyKey, inv.hash, d.ID, now); err != nil {
			return err
		}
	}
	if err := s.Store.RecordTraceCall(ctx, tx, sc, c.TraceID, inv.tool, inv.hash, now); err != nil {
		return err
	}
	d.Outcome = store.OutcomeForwarded
	if approval != nil {
		d.ApprovalID = &approval.ID
		if err := s.Store.Use(ctx, tx, approval.ID, d.ID, now); err != nil {
			return err
		}
		if err := s.attempt(ctx, tx, inv, approval.ID, "executed", d.ID, now); err != nil {
			return err
		}
		if err := s.audit(ctx, tx, inv.p, sc.ProjectID, "approval.used", "approval_request", approval.ID, "",
			map[string]any{"tool": inv.tool, "action_hash": inv.hash, "decision_id": d.ID}); err != nil {
			return err
		}
	}
	v.decision, v.forward = d, true
	return s.Store.InsertDecision(ctx, tx, sc, d)
}

func (s *Server) attempt(ctx context.Context, tx pgx.Tx, inv invocation, approvalID, result, decisionID string, now time.Time) error {
	return s.Store.InsertAttempt(ctx, tx, inv.sc, store.Attempt{ID: ids.New(), ApprovalID: approvalID, Result: result,
		ActionHash: inv.hash, Arguments: inv.redacted, Subject: inv.p.Actor, TraceID: optional(inv.ctx.TraceID),
		DecisionID: &decisionID, CreatedAt: now})
}

// askApproval opens (or reuses) the approval request of the action.
func (s *Server) askApproval(ctx context.Context, tx pgx.Tx, inv invocation, d store.Decision, v *verdict,
	deciding *policy.Compiled, now time.Time) error {
	a, err := s.Store.OpenApproval(ctx, tx, inv.sc, inv.hash, now)
	if errors.Is(err, store.ErrNotFound) {
		expiry := policy.DefaultApprovalExpiry
		if deciding != nil {
			expiry = deciding.Spec.ApprovalExpiry()
		}
		a = store.Approval{ID: ids.New(), Tool: inv.tool, Risk: inv.endpoint.Risk, Agent: inv.ctx.Agent,
			AgentVersion: inv.ctx.AgentVersion, Environment: inv.ctx.Environment, TraceID: d.TraceID,
			ActionHash: inv.hash, Arguments: inv.redacted, Summary: inv.summary, PolicyName: str(d.PolicyName),
			PolicyVersionID: d.PolicyVersionID, Rule: ruleOrDefault(d.Rule), Reason: d.Message, DecisionID: d.ID,
			Status: gateway.StatusPending, RequestedBy: inv.p.Actor,
			ExpiresAt: now.Add(time.Duration(expiry) * time.Second), CreatedAt: now, UpdatedAt: now}
		err = s.Store.InsertApproval(ctx, tx, inv.sc, a)
	}
	if err != nil {
		return err
	}
	d.Outcome, d.ApprovalID = store.OutcomeApprovalRequired, &a.ID
	v.decision = d
	v.refusal = httpx.NewError(http.StatusForbidden, "APPROVAL_REQUIRED", d.Message+" A person must approve this exact action.").
		WithDetails(map[string]any{"decision_id": d.ID, "approval_id": a.ID, "status": gateway.StatusPending,
			"expires_at": a.ExpiresAt.UTC().Format(time.RFC3339), "policy": str(d.PolicyName), "rule": ruleOrDefault(d.Rule)})
	if err := s.Store.InsertDecision(ctx, tx, inv.sc, d); err != nil {
		return err
	}
	return s.violation(ctx, tx, inv, d, d.Message)
}

// useToken checks the approval token of a call that needs approval. The
// approved action runs once; anything else is refused and, when the token
// is valid for the project, recorded on the approval.
func (s *Server) useToken(ctx context.Context, tx pgx.Tx, inv invocation, d store.Decision, v *verdict, now time.Time) error {
	refuse := func(code, message string, details map[string]any) error {
		d.Outcome = store.OutcomeApprovalRefused
		v.decision = d
		if details == nil {
			details = map[string]any{}
		}
		details["decision_id"] = d.ID
		v.refusal = httpx.NewError(http.StatusForbidden, code, message).WithDetails(details)
		if err := s.Store.InsertDecision(ctx, tx, inv.sc, d); err != nil {
			return err
		}
		return s.violation(ctx, tx, inv, d, message)
	}
	a, err := s.Store.ApprovalByToken(ctx, tx, gateway.HashToken(inv.ctx.ApprovalToken))
	if errors.Is(err, store.ErrNotFound) {
		return refuse("APPROVAL_TOKEN_INVALID", "The approval token is not valid.", nil)
	}
	if err != nil {
		return err
	}
	check := gateway.CheckToken(coreApproval(a), inv.ctx.ApprovalToken, inv.sc.OrgID, inv.sc.ProjectID, inv.tool, inv.hash, now)
	if check == nil {
		return s.admit(ctx, tx, inv, d, v, &a, now)
	}
	if errors.Is(check, gateway.ErrTokenInvalid) {
		// Another project's token: nothing is recorded on its approval.
		return refuse("APPROVAL_TOKEN_INVALID", "The approval token is not valid.", nil)
	}
	d.ApprovalID = &a.ID
	details := map[string]any{"approval_id": a.ID}
	var code, message, result string
	switch {
	case errors.Is(check, gateway.ErrApprovalMismatch):
		code, result = "APPROVAL_MISMATCH", "mismatch"
		message = "The approval is for another action; a changed request needs its own approval."
		changes := gateway.DiffArgs(a.Arguments, inv.redacted)
		if a.Tool != inv.tool {
			changes = append([]gateway.Change{{Path: "tool", Before: a.Tool, After: inv.tool}}, changes...)
		}
		if changes == nil {
			changes = []gateway.Change{}
		}
		details["changes"] = changes
	case errors.Is(check, gateway.ErrApprovalUsed):
		code, result, message = "APPROVAL_USED", "used", "The approval has already been used; it lets the action run once."
	case errors.Is(check, gateway.ErrApprovalExpired) && coreApproval(a).Effective(now) != gateway.StatusExpired:
		code, result, message = "APPROVAL_TOKEN_EXPIRED", "expired",
			"The approval token has expired; claim a new one while the approval lasts."
	case errors.Is(check, gateway.ErrApprovalExpired):
		code, result, message = "APPROVAL_EXPIRED", "expired", "The approval has expired; ask for approval again."
		if a.Status != gateway.StatusExpired {
			if err := s.Store.SetStatus(ctx, tx, a.ID, gateway.StatusExpired, now); err != nil {
				return err
			}
		}
	default:
		code, result, message = "APPROVAL_NOT_APPROVED", "not_approved", "The action has not been approved."
	}
	if err := s.attempt(ctx, tx, inv, a.ID, result, d.ID, now); err != nil {
		return err
	}
	return refuse(code, message, details)
}

func ruleOrDefault(rule *string) string {
	if rule == nil || *rule == "" {
		return "default"
	}
	return *rule
}

func str(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// violation publishes a runtime denial or approval requirement.
func (s *Server) violation(ctx context.Context, tx pgx.Tx, inv invocation, d store.Decision, reason string) error {
	decision := string(policy.Deny)
	if d.Effect != nil && *d.Effect == string(policy.RequireApproval) {
		decision = string(policy.RequireApproval)
	}
	return emit(ctx, tx, "policy.violation_detected.v1", inv.sc.OrgID, inv.sc.ProjectID, map[string]any{
		"decision_id": d.ID, "trace_id": d.TraceID, "agent": optional(d.Agent), "agent_version": optional(d.AgentVersion),
		"environment": d.Environment, "tool": d.Tool, "decision": decision, "rule": ruleOrDefault(d.Rule),
		"category": "runtime_policy", "policy_version_id": d.PolicyVersionID, "reason": reason,
		"outcome": d.Outcome, "approval_id": d.ApprovalID,
	})
}

// complete forwards an admitted call, records how it ended and answers with
// what the tool answered.
func (s *Server) complete(ctx context.Context, w http.ResponseWriter, inv invocation, v verdict) error {
	timeout := time.Duration(inv.endpoint.TimeoutMs) * time.Millisecond
	maxBytes := int64(DefaultMaxResponseBytes)
	if l := v.limits; l != nil {
		if l.TimeoutMs > 0 && time.Duration(l.TimeoutMs)*time.Millisecond < timeout {
			timeout = time.Duration(l.TimeoutMs) * time.Millisecond
		}
		if l.MaxResponseBytes > 0 && int64(l.MaxResponseBytes) < maxBytes {
			maxBytes = int64(l.MaxResponseBytes)
		}
	}
	start := time.Now()
	ans, ferr := s.forward(ctx, call{Endpoint: inv.endpoint, Tool: inv.tool, Args: inv.args.Canonical(),
		IdempotencyKey: inv.ctx.IdempotencyKey, Traceparent: inv.traceparent, Headers: inv.headers,
		Timeout: timeout, MaxBytes: maxBytes, RequestID: logx.RequestID(ctx)})
	latency := int(time.Since(start).Milliseconds())
	outcome, code := store.OutcomeExecuted, ""
	var status *int
	switch {
	case ferr != nil:
		outcome, code = store.OutcomeFailed, codeOf(ferr)
		s.Log.WarnContext(ctx, "tool call failed", "tool", inv.tool, "decision_id", v.decision.ID, "error", ferr.Error())
	case ans.Status >= 500:
		outcome, status = store.OutcomeFailed, &ans.Status
	default:
		status = &ans.Status
	}
	statusCode := 0
	if status != nil {
		statusCode = *status
	}
	err := pgx.BeginFunc(ctx, s.Store.Pool, func(tx pgx.Tx) error {
		now := s.now()
		if err := s.Store.CompleteDecision(ctx, tx, v.decision.ID, outcome, status, code, latency, now); err != nil {
			return err
		}
		if inv.ctx.IdempotencyKey == "" {
			return nil
		}
		return s.Store.FinishIdem(ctx, tx, inv.sc, inv.tool, inv.ctx.IdempotencyKey, v.decision.ID,
			gateway.StateAfter(statusCode, ferr), status, ans.Body, ans.ContentType, now)
	})
	if err != nil {
		// The tool has answered: its answer is returned; the record stays
		// forwarded and the key in progress until it goes stale.
		s.Log.ErrorContext(ctx, "recording a completed call failed", "decision_id", v.decision.ID, "error", err.Error())
	}
	if ferr != nil {
		if errors.Is(ferr, errTimeout) {
			return httpx.NewError(http.StatusGatewayTimeout, codeOf(ferr),
				fmt.Sprintf("%s did not answer within %s; its outcome is unknown. Retry with the same idempotency key.", inv.tool, timeout)).
				WithDetails(map[string]any{"decision_id": v.decision.ID})
		}
		return httpx.NewError(http.StatusBadGateway, codeOf(ferr), fmt.Sprintf("%s could not be called (%s).", inv.tool,
			strings.ToLower(strings.ReplaceAll(codeOf(ferr), "_", " ")))).WithDetails(map[string]any{"decision_id": v.decision.ID})
	}
	w.Header().Set("Content-Type", ans.ContentType)
	w.WriteHeader(ans.Status)
	_, _ = w.Write(ans.Body)
	return nil
}

// setDecisionHeaders tells the agent what was decided.
func setDecisionHeaders(h http.Header, d store.Decision, version int) {
	if d.ID == "" {
		return
	}
	h.Set(gateway.HeaderDecisionID, d.ID)
	if d.Effect != nil {
		h.Set(gateway.HeaderDecision, *d.Effect)
	}
	if d.PolicyName != nil {
		h.Set(gateway.HeaderPolicy, *d.PolicyName)
	}
	if version > 0 {
		h.Set(gateway.HeaderPolicyVersion, strconv.Itoa(version))
	}
	if d.Rule != nil {
		h.Set(gateway.HeaderPolicyRule, *d.Rule)
	}
}

// ---------------------------------------------------------------- approvals (agent side)

func (s *Server) gatewayApproval(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "approval_id")
	if err != nil {
		return err
	}
	_, sc, err := gatewayProject(r, r.Header.Get(gateway.HeaderProject))
	if err != nil {
		return err
	}
	a, err := s.Store.Approval(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return found(err, "The approval request")
	}
	httpx.WriteJSON(w, http.StatusOK, approvalOf(a, s.now()))
	return nil
}

type tokenJSON struct {
	ApprovalID string    `json:"approval_id"`
	Token      string    `json:"token"`
	ExpiresAt  time.Time `json:"expires_at"`
}

// claimToken mints the token of an approved request for the caller that
// asked for it. A new token voids the previous one.
func (s *Server) claimToken(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "approval_id")
	if err != nil {
		return err
	}
	p, sc, err := gatewayProject(r, r.Header.Get(gateway.HeaderProject))
	if err != nil {
		return err
	}
	var out tokenJSON
	var refusal error
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		a, err := s.Store.Approval(r.Context(), tx, sc, id, true)
		if err != nil {
			return found(err, "The approval request")
		}
		now := s.now()
		if err := gateway.CanClaim(coreApproval(a), p.Actor, now); err != nil {
			refusal = claimRefusal(err, a)
			if errors.Is(err, gateway.ErrApprovalExpired) && a.Status != gateway.StatusExpired {
				return s.Store.SetStatus(r.Context(), tx, a.ID, gateway.StatusExpired, now)
			}
			return nil
		}
		token, hash, err := gateway.NewToken()
		if err != nil {
			return err
		}
		expires := gateway.TokenExpiry(now, a.ExpiresAt)
		if err := s.Store.SetToken(r.Context(), tx, a.ID, hash, expires, now); err != nil {
			return err
		}
		out = tokenJSON{ApprovalID: a.ID, Token: token, ExpiresAt: expires}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "approval.token_issued", "approval_request", a.ID, "",
			map[string]any{"tool": a.Tool, "action_hash": a.ActionHash, "token_expires_at": expires.Format(time.RFC3339)})
	})
	if err != nil {
		return err
	}
	if refusal != nil {
		return refusal
	}
	w.Header().Set("Cache-Control", "no-store")
	httpx.WriteJSON(w, http.StatusCreated, out)
	return nil
}

func claimRefusal(err error, a store.Approval) error {
	details := map[string]any{"approval_id": a.ID}
	switch {
	case errors.Is(err, gateway.ErrNotRequester):
		return httpx.NewError(http.StatusForbidden, "NOT_REQUESTER", "Only the caller that asked for the approval may claim its token.").
			WithDetails(details)
	case errors.Is(err, gateway.ErrApprovalExpired):
		return httpx.NewError(http.StatusForbidden, "APPROVAL_EXPIRED", "The approval has expired; ask for approval again.").
			WithDetails(details)
	case errors.Is(err, gateway.ErrApprovalUsed):
		return httpx.NewError(http.StatusForbidden, "APPROVAL_USED", "The approval has already been used.").WithDetails(details)
	case a.Status == gateway.StatusDenied:
		details["reason"] = str(a.DecisionReason)
		return httpx.NewError(http.StatusForbidden, "APPROVAL_DENIED", "The action was denied by "+str(a.DecidedBy)+".").
			WithDetails(details)
	default:
		return httpx.NewError(http.StatusConflict, "APPROVAL_PENDING", "The action is waiting for a person's decision.").
			WithDetails(details)
	}
}
