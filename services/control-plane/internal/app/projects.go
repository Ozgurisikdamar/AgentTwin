package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// CreateProjectInput is the request body for project creation.
type CreateProjectInput struct {
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	Description string `json:"description"`
	ContentMode string `json:"content_mode"`
}

var defaultEnvironments = []struct {
	name string
	prod bool
}{{"dev", false}, {"staging", false}, {"production", true}}

// CreateProject creates a project with default environments.
func (a *App) CreateProject(ctx context.Context, p authn.Principal, in CreateProjectInput) (store.Project, error) {
	if err := require(p, authn.PermSettingsWrite); err != nil {
		return store.Project{}, err
	}
	if !slugPattern.MatchString(in.Slug) {
		return store.Project{}, httpx.Invalid("INVALID_PROJECT", "Project slug must be 2-63 lowercase letters, digits or dashes.", map[string]any{"field": "slug"})
	}
	if in.Name == "" || len(in.Name) > 200 {
		return store.Project{}, httpx.Invalid("INVALID_PROJECT", "Project name is required (max 200 characters).", map[string]any{"field": "name"})
	}
	switch in.ContentMode {
	case "", "off", "redacted", "full":
	default:
		return store.Project{}, httpx.Invalid("INVALID_PROJECT", "content_mode must be off, redacted or full.", map[string]any{"field": "content_mode"})
	}
	var proj store.Project
	err := a.Store.Tx(ctx, func(tx pgx.Tx) error {
		n, err := a.Store.CountProjectsForUpdate(ctx, tx, p.OrgID)
		if err != nil {
			return err
		}
		if n >= authn.MaxTokenProjects {
			return httpx.NewError(http.StatusConflict, "PROJECT_LIMIT_REACHED",
				fmt.Sprintf("An organization has at most %d projects.", authn.MaxTokenProjects)).
				WithDetails(map[string]any{"limit": authn.MaxTokenProjects})
		}
		proj, err = a.Store.CreateProject(ctx, tx, p.OrgID, in.Slug, in.Name, in.Description, in.ContentMode, p.Actor)
		if err != nil {
			return err
		}
		for _, e := range defaultEnvironments {
			if _, err := a.Store.EnsureEnvironment(ctx, tx, proj.ID, e.name, e.prod); err != nil {
				return err
			}
		}
		return a.audit(ctx, tx, p, proj.ID, "project.created", "project", proj.ID, nil, proj, "", nil)
	})
	if errors.Is(err, store.ErrConflict) {
		return store.Project{}, httpx.NewError(http.StatusConflict, "PROJECT_EXISTS", fmt.Sprintf("A project with slug %q already exists.", in.Slug))
	}
	return proj, err
}

// ListProjects lists the projects visible to the principal.
func (a *App) ListProjects(ctx context.Context, p authn.Principal) ([]store.Project, error) {
	if err := require(p, authn.PermRead); err != nil {
		return nil, err
	}
	all, err := a.Store.ListProjects(ctx, p.OrgID)
	if err != nil {
		return nil, err
	}
	out := all[:0]
	for _, pr := range all {
		if p.CanAccessProject(pr.ID) {
			if !p.Can(authn.PermSettingsRead) {
				pr.GatePolicy = json.RawMessage(`{}`) // as GetProject
			}
			out = append(out, pr)
		}
	}
	return out, nil
}

// GetProject returns a project (settings visible to settings.read only).
func (a *App) GetProject(ctx context.Context, p authn.Principal, id string) (store.Project, error) {
	if err := requireProject(p, authn.PermRead, id); err != nil {
		return store.Project{}, err
	}
	proj, err := a.Store.GetProject(ctx, p.OrgID, id)
	if err != nil {
		return store.Project{}, notFoundOr(err)
	}
	if !p.Can(authn.PermSettingsRead) {
		proj.GatePolicy = json.RawMessage(`{}`)
	}
	return proj, nil
}

// UpdateProjectSettings changes project settings (admin).
func (a *App) UpdateProjectSettings(ctx context.Context, p authn.Principal, id string, in store.ProjectSettings) (store.Project, error) {
	if err := requireProject(p, authn.PermSettingsWrite, id); err != nil {
		return store.Project{}, err
	}
	if in.Name != nil && (*in.Name == "" || len(*in.Name) > 200) {
		return store.Project{}, httpx.Invalid("INVALID_SETTINGS", "name must be 1-200 characters.", map[string]any{"field": "name"})
	}
	if in.ContentMode != nil {
		switch *in.ContentMode {
		case "off", "redacted", "full":
		default:
			return store.Project{}, httpx.Invalid("INVALID_SETTINGS", "content_mode must be off, redacted or full.", nil)
		}
	}
	for field, v := range map[string]*int{"trace_retention_days": in.TraceRetentionDays, "content_retention_days": in.ContentRetentionDays, "artifact_retention_days": in.ArtifactRetentionDays} {
		if v != nil && (*v < 1 || *v > 3650) {
			return store.Project{}, httpx.Invalid("INVALID_SETTINGS", field+" must be between 1 and 3650.", map[string]any{"field": field})
		}
	}
	if in.GatePolicy != nil {
		if _, err := domain.ParseGatePolicy(*in.GatePolicy); err != nil {
			return store.Project{}, httpx.Invalid("INVALID_GATE_POLICY", err.Error(), nil)
		}
	}
	before, err := a.Store.GetProject(ctx, p.OrgID, id)
	if err != nil {
		return store.Project{}, notFoundOr(err)
	}
	var after store.Project
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		var err error
		after, err = a.Store.UpdateProject(ctx, tx, p.OrgID, id, in, p.Actor)
		if err != nil {
			return err
		}
		return a.audit(ctx, tx, p, id, "project.settings_changed", "project", id, before, after, "", nil)
	})
	return after, notFoundOr(err)
}

// ListEnvironments lists project environments.
func (a *App) ListEnvironments(ctx context.Context, p authn.Principal, projectID string) ([]store.Environment, error) {
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	return a.Store.ListEnvironments(ctx, projectID)
}

// CreateAPIKeyInput is the request body for API key creation.
type CreateAPIKeyInput struct {
	Name          string        `json:"name"`
	Scopes        []authn.Scope `json:"scopes"`
	ExpiresInDays *int          `json:"expires_in_days,omitempty"`
}

// CreatedAPIKey includes the plaintext key exactly once.
type CreatedAPIKey struct {
	store.APIKey
	Key string `json:"key"`
}

func validateKeyInput(in CreateAPIKeyInput) error {
	if in.Name == "" || len(in.Name) > 200 {
		return httpx.Invalid("INVALID_API_KEY", "name is required (max 200 characters).", map[string]any{"field": "name"})
	}
	if len(in.Scopes) == 0 {
		return httpx.Invalid("INVALID_API_KEY", "at least one scope is required.", map[string]any{"field": "scopes"})
	}
	for _, s := range in.Scopes {
		if !authn.ValidScope(s) {
			return httpx.Invalid("INVALID_API_KEY", fmt.Sprintf("unknown scope %q (allowed: traces:write, runtime:invoke, read, ci).", s), map[string]any{"field": "scopes"})
		}
	}
	if in.ExpiresInDays != nil && (*in.ExpiresInDays < 1 || *in.ExpiresInDays > 730) {
		return httpx.Invalid("INVALID_API_KEY", "expires_in_days must be between 1 and 730.", map[string]any{"field": "expires_in_days"})
	}
	return nil
}

// CreateAPIKey creates a project API key and returns the plaintext once.
func (a *App) CreateAPIKey(ctx context.Context, p authn.Principal, projectID string, in CreateAPIKeyInput) (CreatedAPIKey, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAPIKeyManage, projectID); err != nil {
		return CreatedAPIKey{}, err
	}
	if err := validateKeyInput(in); err != nil {
		return CreatedAPIKey{}, err
	}
	var expires *time.Time
	if in.ExpiresInDays != nil {
		t := a.Now().Add(time.Duration(*in.ExpiresInDays) * 24 * time.Hour)
		expires = &t
	}
	return a.insertKey(ctx, p, projectID, in.Name, in.Scopes, expires, nil)
}

func (a *App) insertKey(ctx context.Context, p authn.Principal, projectID, name string, scopes []authn.Scope, expires *time.Time, rotatedFrom *string) (CreatedAPIKey, error) {
	gen, err := auth.GenerateAPIKey(a.Pepper)
	if err != nil {
		return CreatedAPIKey{}, err
	}
	var out CreatedAPIKey
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		k, err := a.Store.InsertAPIKey(ctx, tx, store.APIKey{
			ID: newID(), OrganizationID: p.OrgID, ProjectID: projectID, Name: name, Prefix: gen.Prefix,
			SecretHash: gen.SecretHash, Scopes: scopes, CreatedBy: p.Actor, ExpiresAt: expires, RotatedFrom: rotatedFrom,
		})
		if err != nil {
			return err
		}
		out = CreatedAPIKey{APIKey: k, Key: gen.Plaintext}
		action := "api_key.created"
		if rotatedFrom != nil {
			action = "api_key.rotated"
		}
		// Never put the secret or its hash into the audit log.
		return a.audit(ctx, tx, p, projectID, action, "api_key", k.ID, nil, map[string]any{"prefix": k.Prefix, "scopes": k.Scopes}, "", map[string]any{"prefix": k.Prefix})
	})
	return out, err
}

// ListAPIKeys lists keys of a project.
func (a *App) ListAPIKeys(ctx context.Context, p authn.Principal, projectID string) ([]store.APIKey, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAPIKeyManage, projectID); err != nil {
		return nil, err
	}
	return a.Store.ListAPIKeys(ctx, p.OrgID, projectID)
}

// RevokeAPIKey revokes a key.
func (a *App) RevokeAPIKey(ctx context.Context, p authn.Principal, keyID, reason string) (store.APIKey, error) {
	k, err := a.Store.GetAPIKey(ctx, p.OrgID, keyID)
	if err != nil {
		return store.APIKey{}, notFoundOr(err)
	}
	if err := requireProject(p, authn.PermAPIKeyManage, k.ProjectID); err != nil {
		return store.APIKey{}, err
	}
	var out store.APIKey
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		var err error
		out, err = a.Store.RevokeAPIKey(ctx, tx, p.OrgID, keyID, p.Actor)
		if err != nil {
			return err
		}
		return a.audit(ctx, tx, p, k.ProjectID, "api_key.revoked", "api_key", keyID, nil, nil, reason, map[string]any{"prefix": k.Prefix})
	})
	return out, err
}

// RotateAPIKey issues a replacement with the same name, scopes and expiry and
// revokes the old key. Rotation replaces a secret; it does not extend a key's
// lifetime, so an expired key cannot be rotated.
func (a *App) RotateAPIKey(ctx context.Context, p authn.Principal, keyID string) (CreatedAPIKey, error) {
	old, err := a.Store.GetAPIKey(ctx, p.OrgID, keyID)
	if err != nil {
		return CreatedAPIKey{}, notFoundOr(err)
	}
	if err := requireProject(p, authn.PermAPIKeyManage, old.ProjectID); err != nil {
		return CreatedAPIKey{}, err
	}
	if old.RevokedAt != nil {
		return CreatedAPIKey{}, httpx.NewError(http.StatusConflict, "API_KEY_REVOKED", "A revoked key cannot be rotated; create a new key instead.")
	}
	if old.ExpiresAt != nil && !old.ExpiresAt.After(a.Now()) {
		return CreatedAPIKey{}, httpx.NewError(http.StatusConflict, "API_KEY_EXPIRED", "An expired key cannot be rotated; create a new key instead.")
	}
	created, err := a.insertKey(ctx, p, old.ProjectID, old.Name, old.Scopes, old.ExpiresAt, &old.ID)
	if err != nil {
		return CreatedAPIKey{}, err
	}
	if _, err := a.RevokeAPIKey(ctx, p, old.ID, "rotated"); err != nil {
		return CreatedAPIKey{}, err
	}
	return created, nil
}
