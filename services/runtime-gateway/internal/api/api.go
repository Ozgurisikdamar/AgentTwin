// Package api serves the runtime gateway: the management API (tool
// endpoints, policies and their versions, tests and activation, approvals,
// decisions) under /api/v1 and the invocation path under /gateway/v1
// (ADR-0033). Both sit behind the internal token check; the control plane
// authenticates callers.
package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/netguard"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/migrations"
)

// Producer names this service in the events it writes.
const Producer = "runtime-gateway"

// Server serves the runtime gateway.
type Server struct {
	Store  *store.Store
	Tokens *authn.TokenService
	Log    *slog.Logger
	// Egress is the policy every tool endpoint and every forwarded call must
	// satisfy.
	Egress netguard.Policy
	// Client forwards calls to tools (built from Egress when nil).
	Client *http.Client
	Now    func() time.Time

	policies policyCache
}

// Router is what routes are registered on (an http.ServeMux; a recorder in
// the test that holds them to the API contract).
type Router interface {
	Handle(pattern string, handler http.Handler)
	HandleFunc(pattern string, handler func(http.ResponseWriter, *http.Request))
}

// Routes mounts the API and the invocation path behind the internal token
// check.
func (s *Server) Routes(mux Router) {
	inner := http.NewServeMux()
	s.APIRoutes(inner)
	guarded := httpx.Chain(inner, authn.RequireInternal(s.Tokens, Producer))
	mux.Handle("/api/", guarded)
	mux.Handle("/gateway/", guarded)
}

// APIRoutes registers every route (the management API and the invocation
// path).
func (s *Server) APIRoutes(mux Router) {
	h := httpx.Handle
	mux.HandleFunc("GET /api/v1/tool-endpoints", h(s.listEndpoints))
	mux.HandleFunc("GET /api/v1/tool-endpoints/{tool}", h(s.getEndpoint))
	mux.HandleFunc("PUT /api/v1/tool-endpoints/{tool}", h(s.putEndpoint))
	mux.HandleFunc("DELETE /api/v1/tool-endpoints/{tool}", h(s.deleteEndpoint))

	mux.HandleFunc("GET /api/v1/policies", h(s.listPolicies))
	mux.HandleFunc("POST /api/v1/policies", h(s.createPolicy))
	mux.HandleFunc("POST /api/v1/policies/test", h(s.testPolicy))
	mux.HandleFunc("GET /api/v1/policies/{policy_id}", h(s.getPolicy))
	mux.HandleFunc("POST /api/v1/policies/{policy_id}/versions", h(s.addVersion))
	mux.HandleFunc("GET /api/v1/policies/{policy_id}/versions/{version}", h(s.getVersion))
	mux.HandleFunc("POST /api/v1/policies/{policy_id}/activate", h(s.activate))
	mux.HandleFunc("POST /api/v1/policies/{policy_id}/deactivate", h(s.deactivate))

	mux.HandleFunc("GET /api/v1/approvals", h(s.listApprovals))
	mux.HandleFunc("GET /api/v1/approvals/{approval_id}", h(s.getApproval))
	mux.HandleFunc("POST /api/v1/approvals/{approval_id}/approve", h(s.approve))
	mux.HandleFunc("POST /api/v1/approvals/{approval_id}/deny", h(s.deny))

	mux.HandleFunc("GET /api/v1/policy-decisions", h(s.listDecisions))
	mux.HandleFunc("GET /api/v1/policy-decisions/{decision_id}", h(s.getDecision))

	s.GatewayRoutes(mux)
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now().UTC()
	}
	return time.Now().UTC()
}

// ---------------------------------------------------------------- helpers

// projectOf reads and checks the required project_id.
func projectOf(r *http.Request, raw string, perm authn.Permission) (authn.Principal, store.Scope, error) {
	id := strings.ToLower(raw)
	if id == "" {
		return authn.Principal{}, store.Scope{}, invalidField("project_id", "project_id is required.")
	}
	if !ids.Valid(id) {
		return authn.Principal{}, store.Scope{}, invalidField("project_id", "project_id must be a UUID.")
	}
	p, err := authn.RequireProject(r, perm, id)
	if err != nil {
		return p, store.Scope{}, err
	}
	return p, store.Scope{OrgID: p.OrgID, ProjectID: id}, nil
}

func invalidField(field, message string) *httpx.Error {
	return httpx.Invalid("INVALID_PARAMETER", message, map[string]any{"field": field})
}

// pathID reads a UUID path parameter.
func pathID(r *http.Request, name string) (string, error) {
	id := strings.ToLower(r.PathValue(name))
	if !ids.Valid(id) {
		return "", invalidField(name, name+" must be a UUID.")
	}
	return id, nil
}

var toolName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

func pathTool(r *http.Request) (string, error) {
	tool := r.PathValue("tool")
	if !toolName.MatchString(tool) {
		return "", invalidField("tool", "tool must be a tool name (letters, digits, _ . : -; up to 128 characters).")
	}
	return tool, nil
}

func notFound(what string) *httpx.Error {
	return httpx.NewError(http.StatusNotFound, "NOT_FOUND", what+" does not exist in this project.")
}

// found maps store.ErrNotFound to a 404 naming what is missing.
func found(err error, what string) error {
	if errors.Is(err, store.ErrNotFound) {
		return notFound(what)
	}
	return err
}

// page reads the cursor and limit of a list.
func page(r *http.Request) (*httpx.Cursor, int, error) {
	cur, err := httpx.DecodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		return nil, 0, err
	}
	limit, err := httpx.QueryInt(r, "limit", 50, 1, 200)
	return cur, limit, err
}

// nextCursor is the cursor after the last item of a full page.
func nextCursor[T any](items []T, limit int, key func(T) httpx.Cursor) string {
	if len(items) < limit || len(items) == 0 {
		return ""
	}
	return httpx.EncodeCursor(key(items[len(items)-1]))
}

// audit writes an audit.recorded.v1 event in the transaction.
func (s *Server) audit(ctx context.Context, tx pgx.Tx, p authn.Principal, projectID, action, resourceType, resourceID,
	reason string, metadata map[string]any) error {
	var rid, why any
	if v := logx.RequestID(ctx); v != "" {
		rid = v
	}
	if reason != "" {
		why = reason
	}
	if metadata == nil {
		metadata = map[string]any{}
	}
	env, err := events.New("audit.recorded.v1", Producer, p.OrgID, projectID, logx.RequestID(ctx), "", map[string]any{
		"actor": p.Actor, "action": action, "resource_type": resourceType, "resource_id": resourceID,
		"timestamp": s.now().Format(time.RFC3339Nano), "request_id": rid, "reason": why, "metadata": metadata,
	})
	if err != nil {
		return fmt.Errorf("audit event: %w", err)
	}
	return events.WriteOutbox(ctx, tx, migrations.Schema, env)
}

// emit writes an event in the transaction.
func emit(ctx context.Context, tx pgx.Tx, eventType, orgID, projectID string, payload any) error {
	env, err := events.New(eventType, Producer, orgID, projectID, logx.RequestID(ctx), "", payload)
	if err != nil {
		return fmt.Errorf("%s: %w", eventType, err)
	}
	return events.WriteOutbox(ctx, tx, migrations.Schema, env)
}

// reasonOf checks a person's reason (required, at most 2000 characters).
func reasonOf(reason string) (string, error) {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return "", httpx.Invalid("INVALID_REASON", "A reason is required.", map[string]any{"field": "reason"})
	}
	if len([]rune(reason)) > 2000 {
		return "", httpx.Invalid("INVALID_REASON", "A reason is at most 2000 characters.", map[string]any{"field": "reason"})
	}
	return reason, nil
}
