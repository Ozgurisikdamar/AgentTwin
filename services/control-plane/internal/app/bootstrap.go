package app

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// DemoUser is a seeded development account.
type DemoUser struct {
	Email string
	Name  string
	Role  authn.Role
}

// DemoUsers are the development accounts (AUTH_MODE=dev only).
var DemoUsers = []DemoUser{
	{"owner@demo.agenttwin.dev", "Olivia Owner", authn.RoleOwner},
	{"admin@demo.agenttwin.dev", "Adam Admin", authn.RoleAdmin},
	{"engineer@demo.agenttwin.dev", "Erin Engineer", authn.RoleEngineer},
	{"reviewer@demo.agenttwin.dev", "Riley Reviewer", authn.RoleReviewer},
	{"viewer@demo.agenttwin.dev", "Val Viewer", authn.RoleViewer},
}

// DemoIdentity is the result of BootstrapDemo.
type DemoIdentity struct {
	OrganizationID string
	ProjectID      string
}

// BootstrapDemo creates (idempotently) the demo organization, users, the
// "support" project and the demo API key from AGENTTWIN_DEMO_API_KEY. It is
// only invoked in development mode; production identity comes from OIDC.
func (a *App) BootstrapDemo(ctx context.Context, demoKey string) (DemoIdentity, error) {
	var out DemoIdentity
	err := a.Store.Tx(ctx, func(tx pgx.Tx) error {
		org, err := a.Store.EnsureOrganization(ctx, tx, "demo", "Demo Co")
		if err != nil {
			return err
		}
		out.OrganizationID = org.ID
		for _, du := range DemoUsers {
			u, err := a.Store.EnsureUser(ctx, tx, du.Email, du.Name)
			if err != nil {
				return err
			}
			if err := a.Store.EnsureMembership(ctx, tx, org.ID, u.ID, du.Role); err != nil {
				return err
			}
		}
		// A second organization proves tenant isolation in the demo and tests.
		other, err := a.Store.EnsureOrganization(ctx, tx, "other-co", "Other Co")
		if err != nil {
			return err
		}
		ou, err := a.Store.EnsureUser(ctx, tx, "owner@other.agenttwin.dev", "Oscar Other")
		if err != nil {
			return err
		}
		if err := a.Store.EnsureMembership(ctx, tx, other.ID, ou.ID, authn.RoleOwner); err != nil {
			return err
		}
		proj, err := a.Store.GetProjectBySlug(ctx, tx, org.ID, "support")
		if errors.Is(err, store.ErrNotFound) {
			proj, err = a.Store.CreateProject(ctx, tx, org.ID, "support", "Customer Support", "Support automation agents", "redacted", "system:bootstrap")
		}
		if err != nil {
			return err
		}
		out.ProjectID = proj.ID
		for _, e := range defaultEnvironments {
			if _, err := a.Store.EnsureEnvironment(ctx, tx, proj.ID, e.name, e.prod); err != nil {
				return err
			}
		}
		if demoKey != "" {
			prefix, secret, err := auth.ParseAPIKey(demoKey)
			if err != nil {
				return fmt.Errorf("AGENTTWIN_DEMO_API_KEY: %w", err)
			}
			return a.Store.UpsertDemoAPIKey(ctx, tx, store.APIKey{
				ID: newID(), OrganizationID: org.ID, ProjectID: proj.ID, Name: "demo agent (development)",
				Prefix: prefix, SecretHash: auth.HashSecret(a.Pepper, secret),
				Scopes:    []authn.Scope{authn.ScopeTracesWrite, authn.ScopeRuntimeInvoke, authn.ScopeCI, authn.ScopeRead},
				CreatedBy: "system:bootstrap",
			})
		}
		return nil
	})
	return out, err
}
