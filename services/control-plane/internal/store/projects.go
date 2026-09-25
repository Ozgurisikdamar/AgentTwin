package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

// Project is a product area inside an organization.
type Project struct {
	ID                    string          `json:"id"`
	OrganizationID        string          `json:"organization_id"`
	Slug                  string          `json:"slug"`
	Name                  string          `json:"name"`
	Description           string          `json:"description"`
	ContentMode           string          `json:"content_mode"`
	StorePromptText       bool            `json:"store_prompt_text"`
	TraceRetentionDays    int             `json:"trace_retention_days"`
	ContentRetentionDays  int             `json:"content_retention_days"`
	ArtifactRetentionDays int             `json:"artifact_retention_days"`
	GatePolicy            json.RawMessage `json:"gate_policy"`
	CreatedBy             string          `json:"created_by"`
	UpdatedBy             string          `json:"updated_by"`
	CreatedAt             time.Time       `json:"created_at"`
	UpdatedAt             time.Time       `json:"updated_at"`
}

const projectCols = `id, organization_id, slug, name, description, content_mode, store_prompt_text,
	trace_retention_days, content_retention_days, artifact_retention_days, gate_policy,
	created_by, updated_by, created_at, updated_at`

func scanProject(r pgx.Row) (Project, error) {
	var p Project
	err := r.Scan(&p.ID, &p.OrganizationID, &p.Slug, &p.Name, &p.Description, &p.ContentMode, &p.StorePromptText,
		&p.TraceRetentionDays, &p.ContentRetentionDays, &p.ArtifactRetentionDays, &p.GatePolicy,
		&p.CreatedBy, &p.UpdatedBy, &p.CreatedAt, &p.UpdatedAt)
	return p, mapErr(err)
}

// CreateProject inserts a project; slug must be unique within the organization.
func (s *Store) CreateProject(ctx context.Context, q db.Querier, orgID, slug, name, description, contentMode, actor string) (Project, error) {
	if contentMode == "" {
		contentMode = "off"
	}
	return scanProject(q.QueryRow(ctx, `
		INSERT INTO control.project (id, organization_id, slug, name, description, content_mode, created_by, updated_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
		RETURNING `+projectCols, ids.New(), orgID, slug, name, description, contentMode, actor))
}

// GetProject returns a project of the organization.
func (s *Store) GetProject(ctx context.Context, orgID, id string) (Project, error) {
	return scanProject(s.Pool.QueryRow(ctx, `SELECT `+projectCols+` FROM control.project WHERE organization_id = $1 AND id = $2`, orgID, id))
}

// GetProjectBySlug returns a project by slug.
func (s *Store) GetProjectBySlug(ctx context.Context, q db.Querier, orgID, slug string) (Project, error) {
	return scanProject(q.QueryRow(ctx, `SELECT `+projectCols+` FROM control.project WHERE organization_id = $1 AND slug = $2`, orgID, slug))
}

// GetProjectAnyOrg returns a project by id without tenant scoping. Only for
// internal verification paths where the caller is already bound to the org.
func (s *Store) getProjectUnscoped(ctx context.Context, id string) (Project, error) {
	return scanProject(s.Pool.QueryRow(ctx, `SELECT `+projectCols+` FROM control.project WHERE id = $1`, id))
}

// ListProjects lists projects of an organization.
func (s *Store) ListProjects(ctx context.Context, orgID string) ([]Project, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+projectCols+` FROM control.project WHERE organization_id = $1 ORDER BY slug`, orgID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Project, error) { return scanProject(r) })
}

// ProjectSettings is a partial settings update.
type ProjectSettings struct {
	Name                  *string          `json:"name,omitempty"`
	Description           *string          `json:"description,omitempty"`
	ContentMode           *string          `json:"content_mode,omitempty"`
	StorePromptText       *bool            `json:"store_prompt_text,omitempty"`
	TraceRetentionDays    *int             `json:"trace_retention_days,omitempty"`
	ContentRetentionDays  *int             `json:"content_retention_days,omitempty"`
	ArtifactRetentionDays *int             `json:"artifact_retention_days,omitempty"`
	GatePolicy            *json.RawMessage `json:"gate_policy,omitempty"`
}

// UpdateProject applies settings and returns the updated project.
func (s *Store) UpdateProject(ctx context.Context, q db.Querier, orgID, id string, in ProjectSettings, actor string) (Project, error) {
	var gate []byte
	if in.GatePolicy != nil {
		gate = *in.GatePolicy
	}
	return scanProject(q.QueryRow(ctx, `
		UPDATE control.project SET
			name = COALESCE($3, name),
			description = COALESCE($4, description),
			content_mode = COALESCE($5, content_mode),
			store_prompt_text = COALESCE($6, store_prompt_text),
			trace_retention_days = COALESCE($7, trace_retention_days),
			content_retention_days = COALESCE($8, content_retention_days),
			artifact_retention_days = COALESCE($9, artifact_retention_days),
			gate_policy = COALESCE($10::jsonb, gate_policy),
			updated_by = $11, updated_at = now()
		WHERE organization_id = $1 AND id = $2
		RETURNING `+projectCols,
		orgID, id, in.Name, in.Description, in.ContentMode, in.StorePromptText, in.TraceRetentionDays,
		in.ContentRetentionDays, in.ArtifactRetentionDays, gate, actor))
}

// Environment is a deployment environment label.
type Environment struct {
	ID           string    `json:"id"`
	ProjectID    string    `json:"project_id"`
	Name         string    `json:"name"`
	IsProduction bool      `json:"is_production"`
	CreatedAt    time.Time `json:"created_at"`
}

// EnsureEnvironment upserts an environment.
func (s *Store) EnsureEnvironment(ctx context.Context, q db.Querier, projectID, name string, isProduction bool) (Environment, error) {
	var e Environment
	err := q.QueryRow(ctx, `
		INSERT INTO control.environment (id, project_id, name, is_production) VALUES ($1, $2, $3, $4)
		ON CONFLICT (project_id, name) DO UPDATE SET is_production = EXCLUDED.is_production
		RETURNING id, project_id, name, is_production, created_at`, ids.New(), projectID, name, isProduction).
		Scan(&e.ID, &e.ProjectID, &e.Name, &e.IsProduction, &e.CreatedAt)
	return e, mapErr(err)
}

// ListEnvironments lists environments of a project.
func (s *Store) ListEnvironments(ctx context.Context, projectID string) ([]Environment, error) {
	rows, err := s.Pool.Query(ctx, `SELECT id, project_id, name, is_production, created_at FROM control.environment WHERE project_id = $1 ORDER BY name`, projectID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Environment, error) {
		var e Environment
		err := r.Scan(&e.ID, &e.ProjectID, &e.Name, &e.IsProduction, &e.CreatedAt)
		return e, err
	})
}
