package api

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/gateway"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

var approvalStatuses = []string{gateway.StatusPending, gateway.StatusApproved, gateway.StatusDenied,
	gateway.StatusExpired, gateway.StatusUsed}

// coreApproval is what the approval rules need of a stored request.
func coreApproval(a store.Approval) gateway.Approval {
	g := gateway.Approval{ID: a.ID, Organization: a.Organization, Project: a.Project, Tool: a.Tool,
		ActionHash: a.ActionHash, Status: a.Status, ExpiresAt: a.ExpiresAt, RequestedBy: a.RequestedBy}
	if a.TokenHash != nil {
		g.TokenHash = *a.TokenHash
	}
	if a.TokenExpiresAt != nil {
		g.TokenExpiresAt = *a.TokenExpiresAt
	}
	return g
}

// approvalJSON is a request as people see it: its status as of now (an open
// request past its expiry is EXPIRED).
type approvalJSON struct {
	store.Approval
	Status string `json:"status"`
}

func approvalOf(a store.Approval, now time.Time) approvalJSON {
	return approvalJSON{Approval: a, Status: coreApproval(a).Effective(now)}
}

// attemptJSON is a call that presented the approval's token, with how its
// arguments differ from the approved ones.
type attemptJSON struct {
	store.Attempt
	Changes []gateway.Change `json:"changes"`
}

type approvalDetail struct {
	approvalJSON
	// Decision is the decision that asked for the approval.
	Decision *store.Decision `json:"decision"`
	// UsedDecision is the decision that executed the approved action.
	UsedDecision *store.Decision `json:"used_decision"`
	Attempts     []attemptJSON   `json:"attempts"`
}

func (s *Server) listApprovals(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	_, sc, err := projectOf(r, q.Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	f := store.ApprovalFilter{Status: q.Get("status"), Tool: q.Get("tool")}
	if f.Status != "" && !slices.Contains(approvalStatuses, f.Status) {
		return invalidField("status", "status must be one of PENDING, APPROVED, DENIED, EXPIRED, USED.")
	}
	if f.Tool != "" && !toolName.MatchString(f.Tool) {
		return invalidField("tool", "tool must be a tool name.")
	}
	cur, limit, err := page(r)
	if err != nil {
		return err
	}
	now := s.now()
	list, err := s.Store.Approvals(r.Context(), sc, f, now, cur, limit)
	if err != nil {
		return err
	}
	out := httpx.Page[approvalJSON]{Items: make([]approvalJSON, len(list)),
		NextCursor: nextCursor(list, limit, func(a store.Approval) httpx.Cursor { return httpx.Cursor{TS: a.CreatedAt, ID: a.ID} })}
	for i, a := range list {
		out.Items[i] = approvalOf(a, now)
	}
	httpx.WriteJSON(w, http.StatusOK, out)
	return nil
}

func (s *Server) getApproval(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "approval_id")
	if err != nil {
		return err
	}
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	a, err := s.Store.Approval(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return found(err, "The approval request")
	}
	out := approvalDetail{approvalJSON: approvalOf(a, s.now()), Attempts: []attemptJSON{}}
	if d, err := s.Store.Decision(r.Context(), sc, a.DecisionID); err == nil {
		out.Decision = &d
	} else if !errors.Is(err, store.ErrNotFound) {
		return err
	}
	if a.UsedDecisionID != nil {
		if d, err := s.Store.Decision(r.Context(), sc, *a.UsedDecisionID); err == nil {
			out.UsedDecision = &d
		} else if !errors.Is(err, store.ErrNotFound) {
			return err
		}
	}
	atts, err := s.Store.Attempts(r.Context(), a.ID)
	if err != nil {
		return err
	}
	for _, at := range atts {
		changes := gateway.DiffArgs(a.Arguments, at.Arguments)
		if changes == nil {
			changes = []gateway.Change{}
		}
		out.Attempts = append(out.Attempts, attemptJSON{Attempt: at, Changes: changes})
	}
	httpx.WriteJSON(w, http.StatusOK, out)
	return nil
}

type decideBody struct {
	ProjectID string `json:"project_id"`
	Reason    string `json:"reason"`
}

func (s *Server) approve(w http.ResponseWriter, r *http.Request) error {
	return s.decide(w, r, gateway.StatusApproved)
}

func (s *Server) deny(w http.ResponseWriter, r *http.Request) error {
	return s.decide(w, r, gateway.StatusDenied)
}

// decide records a person's decision on a pending request. A request whose
// time has passed is closed as EXPIRED instead.
func (s *Server) decide(w http.ResponseWriter, r *http.Request, status string) error {
	id, err := pathID(r, "approval_id")
	if err != nil {
		return err
	}
	var b decideBody
	if err := httpx.DecodeJSON(w, r, &b, 16<<10); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermApprovalDecide)
	if err != nil {
		return err
	}
	reason, err := reasonOf(b.Reason)
	if err != nil {
		return err
	}
	var closed error
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		a, err := s.Store.Approval(r.Context(), tx, sc, id, true)
		if err != nil {
			return found(err, "The approval request")
		}
		now := s.now()
		if err := gateway.CanDecide(coreApproval(a), now); err != nil {
			effective := coreApproval(a).Effective(now)
			closed = httpx.NewError(http.StatusConflict, "APPROVAL_CLOSED",
				fmt.Sprintf("The approval request is %s; it can no longer be decided.", effective)).
				WithDetails(map[string]any{"status": effective})
			if a.Status == gateway.StatusPending {
				// Its time passed: record that, then refuse.
				return s.Store.SetStatus(r.Context(), tx, a.ID, gateway.StatusExpired, now)
			}
			return nil
		}
		if err := s.Store.Decide(r.Context(), tx, a.ID, status, p.Actor, reason, now); err != nil {
			return err
		}
		action := "approval.approved"
		if status == gateway.StatusDenied {
			action = "approval.denied"
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, action, "approval_request", a.ID, reason, map[string]any{
			"tool": a.Tool, "action_hash": a.ActionHash, "policy": a.PolicyName, "rule": a.Rule,
			"requested_by": a.RequestedBy, "summary": a.Summary,
		})
	})
	if err != nil {
		return err
	}
	if closed != nil {
		return closed
	}
	a, err := s.Store.Approval(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, approvalOf(a, s.now()))
	return nil
}

// ---------------------------------------------------------------- decisions

var (
	outcomes = []string{store.OutcomeForwarded, store.OutcomeExecuted, store.OutcomeFailed, store.OutcomeReplayed,
		store.OutcomeDenied, store.OutcomeApprovalRequired, store.OutcomeApprovalRefused}
	effects     = []string{"allow", "allow_with_limits", "require_approval", "deny"}
	traceIDForm = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

func (s *Server) listDecisions(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	_, sc, err := projectOf(r, q.Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	f := store.DecisionFilter{Tool: q.Get("tool"), Outcome: q.Get("outcome"), Effect: q.Get("effect"),
		TraceID: q.Get("trace_id"), ApprovalID: strings.ToLower(q.Get("approval_id"))}
	switch {
	case f.Tool != "" && !toolName.MatchString(f.Tool):
		return invalidField("tool", "tool must be a tool name.")
	case f.Outcome != "" && !slices.Contains(outcomes, f.Outcome):
		return invalidField("outcome", "outcome must be one of forwarded, executed, failed, replayed, denied, approval_required, approval_refused.")
	case f.Effect != "" && !slices.Contains(effects, f.Effect):
		return invalidField("effect", "effect must be one of allow, allow_with_limits, require_approval, deny.")
	case f.TraceID != "" && !traceIDForm.MatchString(f.TraceID):
		return invalidField("trace_id", "trace_id must be 32 lowercase hex characters.")
	case f.ApprovalID != "" && !ids.Valid(f.ApprovalID):
		return invalidField("approval_id", "approval_id must be a UUID.")
	}
	cur, limit, err := page(r)
	if err != nil {
		return err
	}
	list, err := s.Store.Decisions(r.Context(), sc, f, cur, limit)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, httpx.Page[store.Decision]{Items: list,
		NextCursor: nextCursor(list, limit, func(d store.Decision) httpx.Cursor { return httpx.Cursor{TS: d.CreatedAt, ID: d.ID} })})
	return nil
}

func (s *Server) getDecision(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "decision_id")
	if err != nil {
		return err
	}
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	d, err := s.Store.Decision(r.Context(), sc, id)
	if err != nil {
		return found(err, "The decision")
	}
	httpx.WriteJSON(w, http.StatusOK, d)
	return nil
}
