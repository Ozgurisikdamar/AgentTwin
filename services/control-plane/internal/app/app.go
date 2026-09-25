// Package app contains the control-plane use cases. Handlers call these
// methods; authorization is enforced here so it is testable without HTTP.
package app

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/svcclient"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// Producer name used in event envelopes.
const Producer = "control-plane"

// App wires use cases to storage.
type App struct {
	Store  *store.Store
	Auth   *auth.Authenticator
	Pepper string
	Log    *slog.Logger
	Now    func() time.Time
	// Graph and Simulation are asked for a change set's impact; nil when
	// the service is not configured.
	Graph, Simulation *svcclient.Client
}

var slugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,62}$`)

func require(p authn.Principal, perm authn.Permission) error {
	if !p.Can(perm) {
		return httpx.ErrForbidden.WithDetails(map[string]any{"required_permission": string(perm)})
	}
	return nil
}

func requireProject(p authn.Principal, perm authn.Permission, projectID string) error {
	if err := require(p, perm); err != nil {
		return err
	}
	if !p.CanAccessProject(projectID) {
		return httpx.ErrNotFound
	}
	return nil
}

// projectFor checks perm and that projectID names a project of the
// principal's organization that the principal may access. Unknown ids and
// projects of other organizations are both NOT_FOUND, so identifiers cannot
// be probed across tenants.
func (a *App) projectFor(ctx context.Context, p authn.Principal, perm authn.Permission, projectID string) (store.Project, error) {
	if err := requireProject(p, perm, projectID); err != nil {
		return store.Project{}, err
	}
	proj, err := a.Store.GetProject(ctx, p.OrgID, projectID)
	if err != nil {
		return store.Project{}, notFoundOr(err)
	}
	return proj, nil
}

func notFoundOr(err error) error {
	if errors.Is(err, store.ErrNotFound) {
		return httpx.ErrNotFound
	}
	return err
}

// audit appends an audit entry for a governance action in the same transaction.
func (a *App) audit(ctx context.Context, tx pgx.Tx, p authn.Principal, projectID, action, resourceType, resourceID string, before, after any, reason string, meta map[string]any) error {
	e := store.AuditFromRequest(ctx, p.OrgID, projectID, p.Actor, action, resourceType, resourceID)
	if before != nil {
		h := hashx.MustOf(before)
		e.BeforeHash = &h
	}
	if after != nil {
		h := hashx.MustOf(after)
		e.AfterHash = &h
	}
	if reason != "" {
		e.Reason = &reason
	}
	e.Metadata = meta
	_, err := a.Store.AppendAudit(ctx, tx, e)
	return err
}

// emit writes an event to the outbox inside tx.
func (a *App) emit(ctx context.Context, tx pgx.Tx, eventType, orgID, projectID string, payload any) error {
	env, err := events.New(eventType, Producer, orgID, projectID, logx.RequestID(ctx), "", payload)
	if err != nil {
		return err
	}
	return events.WriteOutbox(ctx, tx, "control", env)
}

// Me describes the calling principal.
type Me struct {
	Principal    authn.Principal    `json:"principal"`
	User         *store.User        `json:"user,omitempty"`
	Organization store.Organization `json:"organization"`
	Memberships  []store.Membership `json:"memberships,omitempty"`
	Permissions  []string           `json:"permissions"`
}

var allPermissions = []authn.Permission{authn.PermRead, authn.PermSettingsRead, authn.PermSettingsWrite, authn.PermAPIKeyManage,
	authn.PermAgentWrite, authn.PermScenarioWrite, authn.PermSimulationRun, authn.PermEvalRun, authn.PermReleaseWrite,
	authn.PermReleaseOverride, authn.PermReviewWrite, authn.PermRegressionPromote, authn.PermApprovalDecide,
	authn.PermPolicyWrite, authn.PermPolicyActivate, authn.PermPolicyTest, authn.PermTraceWrite, authn.PermRuntimeInvoke, authn.PermGraphWrite}

// WhoAmI returns the principal with its organization and permissions.
func (a *App) WhoAmI(ctx context.Context, p authn.Principal) (Me, error) {
	org, err := a.Store.GetOrganization(ctx, p.OrgID)
	if err != nil {
		return Me{}, notFoundOr(err)
	}
	me := Me{Principal: p, Organization: org, Permissions: []string{}}
	for _, perm := range allPermissions {
		if p.Can(perm) {
			me.Permissions = append(me.Permissions, string(perm))
		}
	}
	if len(p.Actor) > 5 && p.Actor[:5] == "user:" {
		u, err := a.Store.GetUser(ctx, p.Actor[5:])
		if err == nil {
			me.User = &u
			me.Memberships, _ = a.Store.Memberships(ctx, u.ID)
		}
	}
	return me, nil
}

// DevLogin issues a session for a seeded user (AUTH_MODE=dev only).
func (a *App) DevLogin(ctx context.Context, email string) (map[string]any, error) {
	if a.Auth.Mode != auth.ModeDev {
		return nil, httpx.ErrNotFound
	}
	u, err := a.Store.GetUserByEmail(ctx, email)
	if err != nil || u.DisabledAt != nil {
		return nil, httpx.NewError(http.StatusUnauthorized, "UNKNOWN_DEMO_USER", "No demo user with that email. Use one of the seeded demo accounts (e.g. owner@demo.agenttwin.dev).")
	}
	mems, err := a.Store.Memberships(ctx, u.ID)
	if err != nil {
		return nil, err
	}
	if len(mems) == 0 {
		return nil, httpx.ErrForbidden
	}
	tok, exp, err := a.Auth.IssueSession(u, mems[0].OrganizationID)
	if err != nil {
		return nil, err
	}
	return map[string]any{"token": tok, "expires_at": exp.UTC(), "user": u, "organization_id": mems[0].OrganizationID, "role": mems[0].Role}, nil
}

func jsonRaw(v any) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}
