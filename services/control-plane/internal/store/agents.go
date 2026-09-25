package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

// ProjectByIDUnscoped returns a project without tenant scoping; only for
// paths where tenancy was already established (API-key verification).
func (s *Store) ProjectByIDUnscoped(ctx context.Context, id string) (Project, error) {
	return s.getProjectUnscoped(ctx, id)
}

// Agent is a registered agent.
type Agent struct {
	ID             string    `json:"id"`
	OrganizationID string    `json:"organization_id"`
	ProjectID      string    `json:"project_id"`
	Name           string    `json:"name"`
	Description    string    `json:"description"`
	CreatedBy      string    `json:"created_by"`
	CreatedAt      time.Time `json:"created_at"`
	VersionCount   int       `json:"version_count"`
	LatestVersion  *string   `json:"latest_version,omitempty"`
}

const agentCols = `a.id, a.organization_id, a.project_id, a.name, a.description, a.created_by, a.created_at,
	(SELECT count(*) FROM control.agent_version v WHERE v.agent_id = a.id),
	(SELECT v.version FROM control.agent_version v WHERE v.agent_id = a.id ORDER BY v.created_at DESC LIMIT 1)`

func scanAgent(r pgx.Row) (Agent, error) {
	var a Agent
	err := r.Scan(&a.ID, &a.OrganizationID, &a.ProjectID, &a.Name, &a.Description, &a.CreatedBy, &a.CreatedAt, &a.VersionCount, &a.LatestVersion)
	return a, mapErr(err)
}

// EnsureAgent returns the named agent, creating it if absent.
func (s *Store) EnsureAgent(ctx context.Context, q db.Querier, orgID, projectID, name, description, actor string) (Agent, bool, error) {
	var id string
	var inserted bool
	// DO UPDATE (not DO NOTHING) so the statement always returns the row and
	// takes its lock: a concurrent registration of the same agent waits for
	// this transaction instead of reading a snapshot that predates it.
	// xmax = 0 identifies a freshly inserted tuple.
	err := q.QueryRow(ctx, `
		INSERT INTO control.agent (id, organization_id, project_id, name, description, created_by)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (project_id, name) DO UPDATE SET name = EXCLUDED.name
		RETURNING id, (xmax = 0)`, ids.New(), orgID, projectID, name, description, actor).Scan(&id, &inserted)
	if err != nil {
		return Agent{}, false, mapErr(err)
	}
	a, err := scanAgent(q.QueryRow(ctx, `SELECT `+agentCols+` FROM control.agent a WHERE a.organization_id = $1 AND a.id = $2`, orgID, id))
	return a, inserted, err
}

// GetAgent returns an agent of the organization.
func (s *Store) GetAgent(ctx context.Context, orgID, id string) (Agent, error) {
	return scanAgent(s.Pool.QueryRow(ctx, `SELECT `+agentCols+` FROM control.agent a WHERE a.organization_id = $1 AND a.id = $2`, orgID, id))
}

// GetAgentByName returns an agent by project and name.
func (s *Store) GetAgentByName(ctx context.Context, orgID, projectID, name string) (Agent, error) {
	return scanAgent(s.Pool.QueryRow(ctx, `SELECT `+agentCols+` FROM control.agent a WHERE a.organization_id = $1 AND a.project_id = $2 AND a.name = $3`, orgID, projectID, name))
}

// ListAgents lists agents of a project.
func (s *Store) ListAgents(ctx context.Context, orgID, projectID string) ([]Agent, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+agentCols+` FROM control.agent a WHERE a.organization_id = $1 AND a.project_id = $2 ORDER BY a.name`, orgID, projectID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Agent, error) { return scanAgent(r) })
}

// PromptVersion is a stored prompt (content optional).
type PromptVersion struct {
	ID        string    `json:"id"`
	SHA256    string    `json:"sha256"`
	Content   *string   `json:"content,omitempty"`
	SizeBytes int       `json:"size_bytes"`
	CreatedAt time.Time `json:"created_at"`
}

// EnsurePromptVersion stores a prompt by hash (content only when allowed).
func (s *Store) EnsurePromptVersion(ctx context.Context, q db.Querier, agentID, sha string, content *string, size int) (string, error) {
	var id string
	err := q.QueryRow(ctx, `
		INSERT INTO control.prompt_version (id, agent_id, sha256, content, size_bytes) VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (agent_id, sha256) DO UPDATE SET content = COALESCE(control.prompt_version.content, EXCLUDED.content)
		RETURNING id`, ids.New(), agentID, sha, content, size).Scan(&id)
	return id, mapErr(err)
}

// ToolDefinition is the input for a tool version upsert.
type ToolDefinition struct {
	Name              string
	Description       string
	Risk              string
	RiskDimensions    map[string]string
	Compensating      map[string]any
	ApprovalCondition string
	InputSchema       map[string]any
	DefinitionSHA256  string
	Source            string
	SourceRef         string
}

// UpsertToolVersion ensures the tool exists and returns the version matching
// the definition hash, creating a new version when the definition changed.
func (s *Store) UpsertToolVersion(ctx context.Context, q db.Querier, orgID, projectID string, d ToolDefinition, actor string) (toolID, versionID string, version int, created bool, err error) {
	err = q.QueryRow(ctx, `
		INSERT INTO control.tool (id, organization_id, project_id, name, description, current_risk, source)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
		ON CONFLICT (project_id, name) DO UPDATE SET updated_at = now()
		RETURNING id`, ids.New(), orgID, projectID, d.Name, d.Description, d.Risk, d.Source).Scan(&toolID)
	if err != nil {
		return "", "", 0, false, mapErr(err)
	}
	err = q.QueryRow(ctx, `SELECT id, version FROM control.tool_version WHERE tool_id = $1 AND definition_sha256 = $2`, toolID, d.DefinitionSHA256).Scan(&versionID, &version)
	if err == nil {
		return toolID, versionID, version, false, nil
	}
	if !db.IsNoRows(err) {
		return "", "", 0, false, err
	}
	dims, _ := json.Marshal(d.RiskDimensions)
	comp, _ := json.Marshal(d.Compensating)
	schema, _ := json.Marshal(d.InputSchema)
	if d.RiskDimensions == nil {
		dims = nil
	}
	if d.Compensating == nil {
		comp = nil
	}
	if d.InputSchema == nil {
		schema = nil
	}
	err = q.QueryRow(ctx, `
		INSERT INTO control.tool_version (id, tool_id, version, definition_sha256, risk, risk_dimensions, compensating_action,
			approval_condition, input_schema, description, source, source_ref, created_by)
		VALUES ($1, $2, COALESCE((SELECT max(version) FROM control.tool_version WHERE tool_id = $2), 0) + 1,
			$3, $4, $5, $6, NULLIF($7, ''), $8, $9, $10, NULLIF($11, ''), $12)
		RETURNING id, version`,
		ids.New(), toolID, d.DefinitionSHA256, d.Risk, dims, comp, d.ApprovalCondition, schema, d.Description, d.Source, d.SourceRef, actor).
		Scan(&versionID, &version)
	if err != nil {
		return "", "", 0, false, mapErr(err)
	}
	if _, err = q.Exec(ctx, `UPDATE control.tool SET current_risk = $2, description = $3, updated_at = now() WHERE id = $1`, toolID, d.Risk, d.Description); err != nil {
		return "", "", 0, false, err
	}
	return toolID, versionID, version, true, nil
}

// Tool is a tool registry entry with its latest version.
type Tool struct {
	ID            string          `json:"id"`
	ProjectID     string          `json:"project_id"`
	Name          string          `json:"name"`
	Description   string          `json:"description"`
	Risk          string          `json:"risk"`
	Source        string          `json:"source"`
	LatestVersion int             `json:"latest_version"`
	InputSchema   json.RawMessage `json:"input_schema,omitempty"`
	Dimensions    json.RawMessage `json:"risk_dimensions,omitempty"`
	UpdatedAt     time.Time       `json:"updated_at"`
}

// ListTools lists tools of a project.
func (s *Store) ListTools(ctx context.Context, orgID, projectID string) ([]Tool, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT t.id, t.project_id, t.name, t.description, t.current_risk, t.source, COALESCE(v.version, 0), v.input_schema, v.risk_dimensions, t.updated_at
		FROM control.tool t
		LEFT JOIN LATERAL (SELECT version, input_schema, risk_dimensions FROM control.tool_version WHERE tool_id = t.id ORDER BY version DESC LIMIT 1) v ON true
		WHERE t.organization_id = $1 AND t.project_id = $2 ORDER BY t.name`, orgID, projectID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Tool, error) {
		var t Tool
		err := r.Scan(&t.ID, &t.ProjectID, &t.Name, &t.Description, &t.Risk, &t.Source, &t.LatestVersion, &t.InputSchema, &t.Dimensions, &t.UpdatedAt)
		return t, err
	})
}

// AgentVersion is an immutable agent registration.
type AgentVersion struct {
	ID              string          `json:"id"`
	AgentID         string          `json:"agent_id"`
	AgentName       string          `json:"agent_name"`
	ProjectID       string          `json:"project_id"`
	Version         string          `json:"version"`
	Manifest        json.RawMessage `json:"manifest"`
	ManifestSHA256  string          `json:"manifest_sha256"`
	PromptVersionID *string         `json:"prompt_version_id,omitempty"`
	PromptSHA256    *string         `json:"prompt_sha256,omitempty"`
	PromptText      *string         `json:"prompt_text,omitempty"`
	ModelProvider   string          `json:"model_provider"`
	ModelName       string          `json:"model_name"`
	ModelParams     json.RawMessage `json:"model_params"`
	RuntimeEndpoint *string         `json:"runtime_endpoint,omitempty"`
	CommitSHA       *string         `json:"commit_sha,omitempty"`
	RepoURL         *string         `json:"repo_url,omitempty"`
	Branch          *string         `json:"branch,omitempty"`
	CreatedBy       string          `json:"created_by"`
	CreatedAt       time.Time       `json:"created_at"`
}

const versionCols = `v.id, v.agent_id, a.name, a.project_id, v.version, v.manifest, v.manifest_sha256, v.prompt_version_id,
	pv.sha256, pv.content, v.model_provider, v.model_name, v.model_params, v.runtime_endpoint, v.commit_sha, v.repo_url, v.branch,
	v.created_by, v.created_at`

const versionFrom = ` FROM control.agent_version v JOIN control.agent a ON a.id = v.agent_id
	LEFT JOIN control.prompt_version pv ON pv.id = v.prompt_version_id `

func scanVersion(r pgx.Row) (AgentVersion, error) {
	var v AgentVersion
	err := r.Scan(&v.ID, &v.AgentID, &v.AgentName, &v.ProjectID, &v.Version, &v.Manifest, &v.ManifestSHA256, &v.PromptVersionID,
		&v.PromptSHA256, &v.PromptText, &v.ModelProvider, &v.ModelName, &v.ModelParams, &v.RuntimeEndpoint, &v.CommitSHA,
		&v.RepoURL, &v.Branch, &v.CreatedBy, &v.CreatedAt)
	return v, mapErr(err)
}

// InsertAgentVersion stores a new version and links its tool versions.
func (s *Store) InsertAgentVersion(ctx context.Context, q db.Querier, v AgentVersion, toolVersionIDs []string) (string, error) {
	id := ids.New()
	_, err := q.Exec(ctx, `
		INSERT INTO control.agent_version (id, agent_id, version, manifest, manifest_sha256, prompt_version_id, model_provider,
			model_name, model_params, runtime_endpoint, commit_sha, repo_url, branch, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
		id, v.AgentID, v.Version, v.Manifest, v.ManifestSHA256, v.PromptVersionID, v.ModelProvider, v.ModelName, v.ModelParams,
		v.RuntimeEndpoint, v.CommitSHA, v.RepoURL, v.Branch, v.CreatedBy)
	if err != nil {
		return "", mapErr(err)
	}
	for _, tv := range toolVersionIDs {
		if _, err := q.Exec(ctx, `INSERT INTO control.agent_version_tool (agent_version_id, tool_version_id) VALUES ($1, $2) ON CONFLICT DO NOTHING`, id, tv); err != nil {
			return "", err
		}
	}
	return id, nil
}

// GetAgentVersion returns a version by agent and version string.
// Pass the transaction when reading rows written earlier in the same tx.
func (s *Store) GetAgentVersion(ctx context.Context, q db.Querier, orgID, agentID, version string) (AgentVersion, error) {
	return scanVersion(q.QueryRow(ctx, `SELECT `+versionCols+versionFrom+`WHERE a.organization_id = $1 AND v.agent_id = $2 AND v.version = $3`, orgID, agentID, version))
}

// GetAgentVersionByName returns a version by project, agent name and version
// string (simulation runs name the agent version under test).
func (s *Store) GetAgentVersionByName(ctx context.Context, q db.Querier, orgID, projectID, agent, version string) (AgentVersion, error) {
	return scanVersion(q.QueryRow(ctx, `SELECT `+versionCols+versionFrom+`WHERE a.organization_id = $1 AND a.project_id = $2 AND a.name = $3 AND v.version = $4`,
		orgID, projectID, agent, version))
}

// GetAgentVersionByID returns a version by id.
func (s *Store) GetAgentVersionByID(ctx context.Context, q db.Querier, orgID, id string) (AgentVersion, error) {
	return scanVersion(q.QueryRow(ctx, `SELECT `+versionCols+versionFrom+`WHERE a.organization_id = $1 AND v.id = $2`, orgID, id))
}

// ListAgentVersions lists versions newest first.
func (s *Store) ListAgentVersions(ctx context.Context, orgID, agentID string) ([]AgentVersion, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+versionCols+versionFrom+`WHERE a.organization_id = $1 AND v.agent_id = $2 ORDER BY v.created_at DESC`, orgID, agentID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (AgentVersion, error) { return scanVersion(r) })
}

// VersionTool is a tool version bound to an agent version.
type VersionTool struct {
	Name              string          `json:"name"`
	ToolVersion       int             `json:"tool_version"`
	Risk              string          `json:"risk"`
	DefinitionSHA256  string          `json:"definition_sha256"`
	ApprovalCondition *string         `json:"approval_condition,omitempty"`
	InputSchema       json.RawMessage `json:"input_schema,omitempty"`
}

// ListVersionTools lists the tools of an agent version.
func (s *Store) ListVersionTools(ctx context.Context, agentVersionID string) ([]VersionTool, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT t.name, tv.version, tv.risk, tv.definition_sha256, tv.approval_condition, tv.input_schema
		FROM control.agent_version_tool avt
		JOIN control.tool_version tv ON tv.id = avt.tool_version_id
		JOIN control.tool t ON t.id = tv.tool_id
		WHERE avt.agent_version_id = $1 ORDER BY t.name`, agentVersionID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (VersionTool, error) {
		var t VersionTool
		err := r.Scan(&t.Name, &t.ToolVersion, &t.Risk, &t.DefinitionSHA256, &t.ApprovalCondition, &t.InputSchema)
		return t, err
	})
}
