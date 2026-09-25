package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

func newID() string { return ids.New() }

var commitPattern = regexp.MustCompile(`^[a-f0-9]{7,64}$`)

// VersionMetadata is optional source-control metadata for a version.
type VersionMetadata struct {
	CommitSHA string `json:"commit_sha,omitempty"`
	RepoURL   string `json:"repo_url,omitempty"`
	Branch    string `json:"branch,omitempty"`
}

// RegisteredVersion is the result of registering a manifest.
type RegisteredVersion struct {
	Agent   store.Agent        `json:"agent"`
	Version store.AgentVersion `json:"version"`
	Created bool               `json:"created"`
}

// manifestProblem converts a domain.ManifestError into an API error.
func manifestProblem(err error) error {
	var me *domain.ManifestError
	if errors.As(err, &me) {
		return httpx.Invalid("INVALID_MANIFEST", "The agent manifest is invalid.", map[string]any{"problems": me.Problems})
	}
	return err
}

// ValidateManifest validates without persisting (CLI `agent validate`, UI preview).
func (a *App) ValidateManifest(ctx context.Context, p authn.Principal, raw []byte) (map[string]any, error) {
	if err := require(p, authn.PermRead); err != nil {
		return nil, err
	}
	m, err := domain.ParseManifest(raw)
	if err != nil {
		return nil, manifestProblem(err)
	}
	return map[string]any{"valid": true, "name": m.Name, "version": m.Version, "manifest_sha256": m.Hash(), "prompt_sha256": m.PromptSHA256, "normalized": m}, nil
}

// RegisterManifest registers an agent version from a manifest, creating the
// agent and tool registry entries as needed. Versions are immutable:
// re-registering identical content is idempotent, different content under an
// existing version number is a conflict.
func (a *App) RegisterManifest(ctx context.Context, p authn.Principal, projectID string, raw []byte, meta VersionMetadata) (RegisteredVersion, error) {
	if err := requireProject(p, authn.PermAgentWrite, projectID); err != nil {
		return RegisteredVersion{}, err
	}
	if meta.CommitSHA != "" && !commitPattern.MatchString(meta.CommitSHA) {
		return RegisteredVersion{}, httpx.Invalid("INVALID_COMMIT", "commit_sha must be a lowercase hex git sha (7-64 chars).", map[string]any{"field": "commit_sha"})
	}
	m, err := domain.ParseManifest(raw)
	if err != nil {
		return RegisteredVersion{}, manifestProblem(err)
	}
	proj, err := a.Store.GetProject(ctx, p.OrgID, projectID)
	if err != nil {
		return RegisteredVersion{}, notFoundOr(err)
	}
	var out RegisteredVersion
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		agent, _, err := a.Store.EnsureAgent(ctx, tx, p.OrgID, projectID, m.Name, m.Description, p.Actor)
		if err != nil {
			return err
		}
		existing, err := a.Store.GetAgentVersion(ctx, tx, p.OrgID, agent.ID, m.Version)
		if err == nil {
			if existing.ManifestSHA256 == m.Hash() {
				out = RegisteredVersion{Agent: agent, Version: existing, Created: false}
				return nil
			}
			return httpx.NewError(http.StatusConflict, "VERSION_EXISTS",
				fmt.Sprintf("%s@%s is already registered with different content. Versions are immutable; bump the version.", m.Name, m.Version))
		}
		if !errors.Is(err, store.ErrNotFound) {
			return err
		}
		var promptID *string
		if m.PromptSHA256 != "" {
			var content *string
			if proj.StorePromptText && m.Instructions != "" {
				c := m.Instructions
				content = &c
			}
			id, err := a.Store.EnsurePromptVersion(ctx, tx, agent.ID, m.PromptSHA256, content, len(m.Instructions))
			if err != nil {
				return err
			}
			promptID = &id
		}
		var toolVersionIDs []string
		type toolPayload struct {
			Name        string `json:"name"`
			Risk        string `json:"risk"`
			ToolVersion int    `json:"tool_version"`
		}
		var tools []toolPayload
		for _, t := range m.Tools {
			_, tvID, tv, _, err := a.Store.UpsertToolVersion(ctx, tx, p.OrgID, projectID, store.ToolDefinition{
				Name: t.Name, Description: t.Description, Risk: string(t.Risk), RiskDimensions: t.Dimensions,
				Compensating: t.Compensating, ApprovalCondition: t.ApprovalWhen, InputSchema: t.InputSchema,
				DefinitionSHA256: t.DefinitionSHA, Source: "MANIFEST", SourceRef: m.Name + "@" + m.Version,
			}, p.Actor)
			if err != nil {
				return err
			}
			toolVersionIDs = append(toolVersionIDs, tvID)
			tools = append(tools, toolPayload{Name: t.Name, Risk: string(t.Risk), ToolVersion: tv})
		}
		normalized, _ := json.Marshal(m)
		params := map[string]any{}
		if m.Model.Temperature != nil {
			params["temperature"] = *m.Model.Temperature
		}
		if m.Model.MaxTokens != nil {
			params["max_tokens"] = *m.Model.MaxTokens
		}
		for k, v := range m.Model.Params {
			params[k] = v
		}
		v := store.AgentVersion{
			AgentID: agent.ID, Version: m.Version, Manifest: normalized, ManifestSHA256: m.Hash(),
			PromptVersionID: promptID, ModelProvider: m.Model.Provider, ModelName: m.Model.Name, ModelParams: jsonRaw(params),
			CreatedBy: p.Actor,
		}
		if m.RuntimeEndpoint != "" {
			v.RuntimeEndpoint = &m.RuntimeEndpoint
		}
		if meta.CommitSHA != "" {
			v.CommitSHA = &meta.CommitSHA
		}
		if meta.RepoURL != "" {
			v.RepoURL = &meta.RepoURL
		}
		if meta.Branch != "" {
			v.Branch = &meta.Branch
		}
		versionID, err := a.Store.InsertAgentVersion(ctx, tx, v, toolVersionIDs)
		if err != nil {
			return err
		}
		deps := make([]map[string]any, 0, len(m.Dependencies))
		for _, d := range m.Dependencies {
			deps = append(deps, map[string]any{"tool": d.Tool, "kind": d.Kind, "name": d.Name, "relation": d.Relation, "criticality": d.Critical})
		}
		if err := a.emit(ctx, tx, "agent.version_registered.v1", p.OrgID, projectID, map[string]any{
			"agent_id": agent.ID, "agent_name": agent.Name, "version_id": versionID, "version": m.Version,
			"manifest_hash": m.Hash(), "prompt_hash": nilIfEmpty(m.PromptSHA256),
			"model":             map[string]any{"provider": m.Model.Provider, "name": m.Model.Name},
			"tools":             tools,
			"retrieval_sources": m.RetrievalSources,
			"dependencies":      deps,
		}); err != nil {
			return err
		}
		if err := a.audit(ctx, tx, p, projectID, "agent_version.created", "agent_version", versionID, nil,
			map[string]any{"manifest_sha256": m.Hash()}, "", map[string]any{"agent": m.Name, "version": m.Version}); err != nil {
			return err
		}
		stored, err := a.Store.GetAgentVersionByID(ctx, tx, p.OrgID, versionID)
		if err != nil {
			return err
		}
		agent.VersionCount++
		agent.LatestVersion = &m.Version
		out = RegisteredVersion{Agent: agent, Version: stored, Created: true}
		return nil
	})
	if errors.Is(err, store.ErrConflict) {
		// A concurrent registration of the same version won the race: answer
		// exactly as if this request had arrived second.
		agent, aerr := a.Store.GetAgentByName(ctx, p.OrgID, projectID, m.Name)
		if aerr != nil {
			return RegisteredVersion{}, notFoundOr(aerr)
		}
		existing, verr := a.Store.GetAgentVersion(ctx, a.Store.Pool, p.OrgID, agent.ID, m.Version)
		if verr != nil {
			return RegisteredVersion{}, verr
		}
		if existing.ManifestSHA256 == m.Hash() {
			return RegisteredVersion{Agent: agent, Version: existing, Created: false}, nil
		}
		return RegisteredVersion{}, httpx.NewError(http.StatusConflict, "VERSION_EXISTS",
			fmt.Sprintf("%s@%s is already registered with different content. Versions are immutable; bump the version.", m.Name, m.Version))
	}
	return out, err
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// CreateAgentInput creates an agent shell without versions.
type CreateAgentInput struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

var agentName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// CreateAgent creates an agent (no version yet).
func (a *App) CreateAgent(ctx context.Context, p authn.Principal, projectID string, in CreateAgentInput) (store.Agent, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAgentWrite, projectID); err != nil {
		return store.Agent{}, err
	}
	if !agentName.MatchString(in.Name) {
		return store.Agent{}, httpx.Invalid("INVALID_AGENT", "Agent name must be lowercase letters, digits, '-' or '_' (max 63).", map[string]any{"field": "name"})
	}
	var out store.Agent
	err := a.Store.Tx(ctx, func(tx pgx.Tx) error {
		ag, inserted, err := a.Store.EnsureAgent(ctx, tx, p.OrgID, projectID, in.Name, in.Description, p.Actor)
		if err != nil {
			return err
		}
		if !inserted {
			return httpx.NewError(http.StatusConflict, "AGENT_EXISTS", fmt.Sprintf("Agent %q already exists in this project.", in.Name))
		}
		out = ag
		return a.audit(ctx, tx, p, projectID, "agent.created", "agent", ag.ID, nil, ag, "", nil)
	})
	return out, err
}

// ListAgents lists agents of a project.
func (a *App) ListAgents(ctx context.Context, p authn.Principal, projectID string) ([]store.Agent, error) {
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	return a.Store.ListAgents(ctx, p.OrgID, projectID)
}

// GetAgent returns an agent.
func (a *App) GetAgent(ctx context.Context, p authn.Principal, id string) (store.Agent, error) {
	ag, err := a.Store.GetAgent(ctx, p.OrgID, id)
	if err != nil {
		return store.Agent{}, notFoundOr(err)
	}
	if err := requireProject(p, authn.PermRead, ag.ProjectID); err != nil {
		return store.Agent{}, err
	}
	return ag, nil
}

// VersionDetail is a version with its bound tools.
type VersionDetail struct {
	store.AgentVersion
	Tools []store.VersionTool `json:"tools"`
}

func (a *App) versionVisibility(p authn.Principal, v *store.AgentVersion) {
	// Prompt text is configuration; hide it from roles without settings.read.
	if !p.Can(authn.PermSettingsRead) {
		v.PromptText = nil
	}
}

// ListVersions lists versions of an agent.
func (a *App) ListVersions(ctx context.Context, p authn.Principal, agentID string) ([]store.AgentVersion, error) {
	if _, err := a.GetAgent(ctx, p, agentID); err != nil {
		return nil, err
	}
	vs, err := a.Store.ListAgentVersions(ctx, p.OrgID, agentID)
	for i := range vs {
		a.versionVisibility(p, &vs[i])
	}
	return vs, err
}

// GetVersion returns one version with tools.
func (a *App) GetVersion(ctx context.Context, p authn.Principal, agentID, version string) (VersionDetail, error) {
	if _, err := a.GetAgent(ctx, p, agentID); err != nil {
		return VersionDetail{}, err
	}
	v, err := a.Store.GetAgentVersion(ctx, a.Store.Pool, p.OrgID, agentID, version)
	if err != nil {
		return VersionDetail{}, notFoundOr(err)
	}
	a.versionVisibility(p, &v)
	tools, err := a.Store.ListVersionTools(ctx, v.ID)
	if err != nil {
		return VersionDetail{}, err
	}
	return VersionDetail{AgentVersion: v, Tools: tools}, nil
}

// ListTools lists the project's tool registry.
func (a *App) ListTools(ctx context.Context, p authn.Principal, projectID string) ([]store.Tool, error) {
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	return a.Store.ListTools(ctx, p.OrgID, projectID)
}

// CreateToolInput registers a tool manually.
type CreateToolInput struct {
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Risk        domain.RiskLevel  `json:"risk"`
	Dimensions  map[string]string `json:"risk_dimensions,omitempty"`
	InputSchema map[string]any    `json:"input_schema,omitempty"`
}

// CreateTool adds a manual tool (or a new version of it).
func (a *App) CreateTool(ctx context.Context, p authn.Principal, projectID string, in CreateToolInput) (map[string]any, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAgentWrite, projectID); err != nil {
		return nil, err
	}
	if !agentName.MatchString(in.Name) {
		return nil, httpx.Invalid("INVALID_TOOL", "Tool name must be lowercase letters, digits, '-' or '_'.", map[string]any{"field": "name"})
	}
	if in.Risk == "" {
		in.Risk = domain.DefaultRisk
	}
	switch in.Risk {
	case domain.RiskRead, domain.RiskWriteReversible, domain.RiskWriteIrreversible, domain.RiskExecute, domain.RiskAdmin:
	default:
		return nil, httpx.Invalid("INVALID_TOOL", "risk must be READ, WRITE_REVERSIBLE, WRITE_IRREVERSIBLE, EXECUTE or ADMIN.", map[string]any{"field": "risk"})
	}
	if _, err := a.Store.GetProject(ctx, p.OrgID, projectID); err != nil {
		return nil, notFoundOr(err)
	}
	def := store.ToolDefinition{Name: in.Name, Description: in.Description, Risk: string(in.Risk), RiskDimensions: in.Dimensions, InputSchema: in.InputSchema, Source: "MANUAL"}
	def.DefinitionSHA256 = hashDefinition(def)
	var res map[string]any
	err := a.Store.Tx(ctx, func(tx pgx.Tx) error {
		toolID, _, version, created, err := a.Store.UpsertToolVersion(ctx, tx, p.OrgID, projectID, def, p.Actor)
		if err != nil {
			return err
		}
		res = map[string]any{"tool_id": toolID, "name": in.Name, "version": version, "created": created}
		if !created {
			return nil
		}
		if err := a.emit(ctx, tx, "tool.catalog_imported.v1", p.OrgID, projectID, map[string]any{
			"source": "MANUAL", "source_name": "manual", "tools": []map[string]any{{"name": in.Name, "risk": string(in.Risk), "mutating": in.Risk != domain.RiskRead}},
		}); err != nil {
			return err
		}
		return a.audit(ctx, tx, p, projectID, "tool.registered", "tool", toolID, nil, def, "", map[string]any{"version": version})
	})
	return res, err
}

func hashDefinition(d store.ToolDefinition) string {
	return mustHash(map[string]any{"risk": d.Risk, "dimensions": d.RiskDimensions, "input_schema": d.InputSchema, "description": d.Description, "source": d.Source})
}
