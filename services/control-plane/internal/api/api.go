// Package api is the control-plane HTTP layer: routing, request decoding and
// response rendering. Use cases live in package app.
package api

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/app"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/proxy"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// Server holds dependencies for handlers.
type Server struct {
	App      *app.App
	Auth     *auth.Authenticator
	Tokens   *authn.TokenService
	Proxy    *proxy.Proxy
	Log      *slog.Logger
	Extra    func(mux *http.ServeMux) // later phases register more routes
	AuthMode auth.Mode
	OIDC     map[string]string
}

func principal(r *http.Request) authn.Principal {
	p, _ := authn.FromContext(r.Context())
	return p
}

// PublicRoutes registers the public API on mux (behind authentication).
func (s *Server) PublicRoutes(mux *http.ServeMux) {
	h := httpx.Handle
	mux.HandleFunc("GET /api/v1/auth/config", h(s.authConfig))
	mux.HandleFunc("POST /api/v1/auth/dev/login", h(s.devLogin))
	mux.HandleFunc("GET /api/v1/me", h(s.me))
	mux.HandleFunc("GET /api/v1/members", h(s.members))

	mux.HandleFunc("POST /api/v1/projects", h(s.createProject))
	mux.HandleFunc("GET /api/v1/projects", h(s.listProjects))
	mux.HandleFunc("GET /api/v1/projects/{id}", h(s.getProject))
	mux.HandleFunc("PATCH /api/v1/projects/{id}", h(s.updateProject))
	mux.HandleFunc("GET /api/v1/projects/{id}/environments", h(s.listEnvironments))

	mux.HandleFunc("POST /api/v1/projects/{id}/api-keys", h(s.createAPIKey))
	mux.HandleFunc("GET /api/v1/projects/{id}/api-keys", h(s.listAPIKeys))
	mux.HandleFunc("POST /api/v1/api-keys/{id}/revoke", h(s.revokeAPIKey))
	mux.HandleFunc("POST /api/v1/api-keys/{id}/rotate", h(s.rotateAPIKey))

	mux.HandleFunc("POST /api/v1/projects/{id}/agents", h(s.createAgent))
	mux.HandleFunc("GET /api/v1/projects/{id}/agents", h(s.listAgents))
	mux.HandleFunc("POST /api/v1/projects/{id}/agent-manifests", h(s.registerManifest))
	mux.HandleFunc("POST /api/v1/manifests/validate", h(s.validateManifest))
	mux.HandleFunc("GET /api/v1/agents/{id}", h(s.getAgent))
	mux.HandleFunc("POST /api/v1/agents/{id}/versions", h(s.createVersion))
	mux.HandleFunc("GET /api/v1/agents/{id}/versions", h(s.listVersions))
	mux.HandleFunc("GET /api/v1/agents/{id}/versions/{version}", h(s.getVersion))

	mux.HandleFunc("GET /api/v1/projects/{id}/tools", h(s.listTools))
	mux.HandleFunc("POST /api/v1/projects/{id}/tools", h(s.createTool))

	mux.HandleFunc("GET /api/v1/audit", h(s.listAudit))
	mux.HandleFunc("GET /api/v1/audit/verify", h(s.verifyAudit))

	if s.Extra != nil {
		s.Extra(mux)
	}
	// Everything else under /api/v1 is owned by an internal service.
	mux.Handle("/api/v1/", s.Proxy)
}

// IsPublic lists unauthenticated endpoints.
func IsPublic(r *http.Request) bool {
	switch r.URL.Path {
	case "/api/v1/auth/config", "/api/v1/auth/dev/login", "/health/live", "/health/ready", "/metrics":
		return true
	}
	return false
}

func (s *Server) authConfig(w http.ResponseWriter, r *http.Request) error {
	out := map[string]any{"mode": s.AuthMode}
	if s.AuthMode == auth.ModeOIDC {
		out["oidc"] = s.OIDC
	}
	if s.AuthMode == auth.ModeDev {
		users := []map[string]string{}
		for _, u := range app.DemoUsers {
			users = append(users, map[string]string{"email": u.Email, "name": u.Name, "role": string(u.Role)})
		}
		out["demo_users"] = users
	}
	httpx.WriteJSON(w, http.StatusOK, out)
	return nil
}

func (s *Server) devLogin(w http.ResponseWriter, r *http.Request) error {
	var in struct {
		Email string `json:"email"`
	}
	if err := httpx.DecodeJSON(w, r, &in, 4096); err != nil {
		return err
	}
	res, err := s.App.DevLogin(r.Context(), strings.TrimSpace(in.Email))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, res)
	return nil
}

func (s *Server) me(w http.ResponseWriter, r *http.Request) error {
	me, err := s.App.WhoAmI(r.Context(), principal(r))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, me)
	return nil
}

func (s *Server) members(w http.ResponseWriter, r *http.Request) error {
	p := principal(r)
	if !p.Can(authn.PermSettingsRead) {
		return httpx.ErrForbidden
	}
	ms, err := s.App.Store.ListMembers(r.Context(), p.OrgID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ms})
	return nil
}

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) error {
	var in app.CreateProjectInput
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	pr, err := s.App.CreateProject(r.Context(), principal(r), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, pr)
	return nil
}

func (s *Server) listProjects(w http.ResponseWriter, r *http.Request) error {
	ps, err := s.App.ListProjects(r.Context(), principal(r))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ps})
	return nil
}

func (s *Server) getProject(w http.ResponseWriter, r *http.Request) error {
	pr, err := s.App.GetProject(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pr)
	return nil
}

func (s *Server) updateProject(w http.ResponseWriter, r *http.Request) error {
	var in store.ProjectSettings
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	pr, err := s.App.UpdateProjectSettings(r.Context(), principal(r), r.PathValue("id"), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pr)
	return nil
}

func (s *Server) listEnvironments(w http.ResponseWriter, r *http.Request) error {
	es, err := s.App.ListEnvironments(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": es})
	return nil
}

func (s *Server) createAPIKey(w http.ResponseWriter, r *http.Request) error {
	var in app.CreateAPIKeyInput
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	k, err := s.App.CreateAPIKey(r.Context(), principal(r), r.PathValue("id"), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, k)
	return nil
}

func (s *Server) listAPIKeys(w http.ResponseWriter, r *http.Request) error {
	ks, err := s.App.ListAPIKeys(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ks})
	return nil
}

func (s *Server) revokeAPIKey(w http.ResponseWriter, r *http.Request) error {
	var in struct {
		Reason string `json:"reason"`
	}
	if r.ContentLength > 0 {
		if err := httpx.DecodeJSON(w, r, &in, 4096); err != nil {
			return err
		}
	}
	k, err := s.App.RevokeAPIKey(r.Context(), principal(r), r.PathValue("id"), in.Reason)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, k)
	return nil
}

func (s *Server) rotateAPIKey(w http.ResponseWriter, r *http.Request) error {
	k, err := s.App.RotateAPIKey(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, k)
	return nil
}

func (s *Server) createAgent(w http.ResponseWriter, r *http.Request) error {
	var in app.CreateAgentInput
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	a, err := s.App.CreateAgent(r.Context(), principal(r), r.PathValue("id"), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, a)
	return nil
}

func (s *Server) listAgents(w http.ResponseWriter, r *http.Request) error {
	as, err := s.App.ListAgents(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": as})
	return nil
}

// readManifestRequest accepts either a raw YAML/JSON manifest (commit metadata
// in query parameters) or a JSON envelope {"manifest": ..., "commit_sha": ...}.
func readManifestRequest(w http.ResponseWriter, r *http.Request) ([]byte, app.VersionMetadata, error) {
	body, err := httpx.ReadLimited(w, r, domain.MaxManifestBytes+4096)
	if err != nil {
		return nil, app.VersionMetadata{}, err
	}
	meta := app.VersionMetadata{CommitSHA: r.URL.Query().Get("commit_sha"), RepoURL: r.URL.Query().Get("repo_url"), Branch: r.URL.Query().Get("branch")}
	ct := strings.ToLower(r.Header.Get("Content-Type"))
	if strings.HasPrefix(ct, "application/json") {
		var env struct {
			Manifest  json.RawMessage `json:"manifest"`
			CommitSHA string          `json:"commit_sha"`
			RepoURL   string          `json:"repo_url"`
			Branch    string          `json:"branch"`
		}
		if err := json.Unmarshal(body, &env); err == nil && len(env.Manifest) > 0 {
			var asString string
			raw := []byte(env.Manifest)
			if json.Unmarshal(env.Manifest, &asString) == nil {
				raw = []byte(asString)
			}
			if env.CommitSHA != "" {
				meta.CommitSHA = env.CommitSHA
			}
			if env.RepoURL != "" {
				meta.RepoURL = env.RepoURL
			}
			if env.Branch != "" {
				meta.Branch = env.Branch
			}
			return raw, meta, nil
		}
	}
	return body, meta, nil
}

func (s *Server) registerManifest(w http.ResponseWriter, r *http.Request) error {
	raw, meta, err := readManifestRequest(w, r)
	if err != nil {
		return err
	}
	res, err := s.App.RegisterManifest(r.Context(), principal(r), r.PathValue("id"), raw, meta)
	if err != nil {
		return err
	}
	status := http.StatusCreated
	if !res.Created {
		status = http.StatusOK
	}
	httpx.WriteJSON(w, status, res)
	return nil
}

func (s *Server) validateManifest(w http.ResponseWriter, r *http.Request) error {
	raw, _, err := readManifestRequest(w, r)
	if err != nil {
		return err
	}
	res, err := s.App.ValidateManifest(r.Context(), principal(r), raw)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, res)
	return nil
}

func (s *Server) getAgent(w http.ResponseWriter, r *http.Request) error {
	a, err := s.App.GetAgent(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, a)
	return nil
}

func (s *Server) createVersion(w http.ResponseWriter, r *http.Request) error {
	agent, err := s.App.GetAgent(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	raw, meta, err := readManifestRequest(w, r)
	if err != nil {
		return err
	}
	m, err := domain.ParseManifest(raw)
	if err == nil && m.Name != agent.Name {
		return httpx.Invalid("MANIFEST_AGENT_MISMATCH", "Manifest metadata.name does not match this agent.", map[string]any{"agent": agent.Name, "manifest": m.Name})
	}
	res, err := s.App.RegisterManifest(r.Context(), principal(r), agent.ProjectID, raw, meta)
	if err != nil {
		return err
	}
	status := http.StatusCreated
	if !res.Created {
		status = http.StatusOK
	}
	httpx.WriteJSON(w, status, res)
	return nil
}

func (s *Server) listVersions(w http.ResponseWriter, r *http.Request) error {
	vs, err := s.App.ListVersions(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": vs})
	return nil
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) error {
	v, err := s.App.GetVersion(r.Context(), principal(r), r.PathValue("id"), r.PathValue("version"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, v)
	return nil
}

func (s *Server) listTools(w http.ResponseWriter, r *http.Request) error {
	ts, err := s.App.ListTools(r.Context(), principal(r), r.PathValue("id"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ts})
	return nil
}

func (s *Server) createTool(w http.ResponseWriter, r *http.Request) error {
	var in app.CreateToolInput
	if err := httpx.DecodeJSON(w, r, &in, 256<<10); err != nil {
		return err
	}
	res, err := s.App.CreateTool(r.Context(), principal(r), r.PathValue("id"), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, res)
	return nil
}

func (s *Server) listAudit(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	limit, err := httpx.QueryInt(r, "limit", 50, 1, 200)
	if err != nil {
		return err
	}
	var before int64
	if c := q.Get("cursor"); c != "" {
		before, err = strconv.ParseInt(c, 10, 64)
		if err != nil || before < 0 {
			return httpx.Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
		}
	}
	items, err := s.App.ListAudit(r.Context(), principal(r), store.AuditFilter{
		ProjectID: q.Get("project_id"), ResourceType: q.Get("resource_type"), ResourceID: q.Get("resource_id"),
		Action: q.Get("action"), BeforeSeq: before, Limit: limit,
	})
	if err != nil {
		return err
	}
	next := ""
	if len(items) == limit {
		next = strconv.FormatInt(items[len(items)-1].Seq, 10)
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items, "next_cursor": next})
	return nil
}

func (s *Server) verifyAudit(w http.ResponseWriter, r *http.Request) error {
	rep, err := s.App.VerifyAudit(r.Context(), principal(r))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, rep)
	return nil
}

// InternalRoutes registers /internal/v1 endpoints (internal JWT required).
func (s *Server) InternalRoutes(mux *http.ServeMux) {
	h := httpx.Handle
	mux.HandleFunc("POST /internal/v1/api-keys/verify", h(s.internalVerifyKey))
	mux.HandleFunc("GET /internal/v1/projects/{id}", h(s.internalProject))
	mux.HandleFunc("GET /internal/v1/agent-versions/{id}", h(s.internalAgentVersion))
	mux.HandleFunc("GET /internal/v1/agent-versions", h(s.internalAgentVersionByName))
}

// internalVerifyKey lets trace-service and runtime-gateway authenticate API
// keys without access to the control schema.
func (s *Server) internalVerifyKey(w http.ResponseWriter, r *http.Request) error {
	caller := principal(r)
	if caller.Role != authn.RoleService {
		return httpx.ErrForbidden
	}
	var in struct {
		Key string `json:"key"`
	}
	if err := httpx.DecodeJSON(w, r, &in, 4096); err != nil {
		return err
	}
	k, err := s.Auth.VerifyAPIKey(r.Context(), in.Key)
	if err != nil {
		if errors.Is(err, auth.ErrInvalidCredentials) {
			httpx.WriteJSON(w, http.StatusOK, map[string]any{"valid": false})
			return nil
		}
		return err
	}
	proj, err := s.App.Store.ProjectByIDUnscoped(r.Context(), k.ProjectID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{
		"valid": true, "key_id": k.ID, "organization_id": k.OrganizationID, "project_id": k.ProjectID,
		"scopes": k.Scopes, "content_mode": proj.ContentMode, "trace_retention_days": proj.TraceRetentionDays,
		"content_retention_days": proj.ContentRetentionDays,
	})
	return nil
}

func (s *Server) internalProject(w http.ResponseWriter, r *http.Request) error {
	p := principal(r)
	id := r.PathValue("id")
	var (
		pr  store.Project
		err error
	)
	switch {
	case p.Role == authn.RoleService && p.OrgID == authn.SystemOrg:
		// Platform-level lookups (e.g. trace ingestion resolving an API key's
		// project settings) happen before an organization is known.
		pr, err = s.App.Store.ProjectByIDUnscoped(r.Context(), id)
	case p.Role == authn.RoleService || p.CanAccessProject(id):
		pr, err = s.App.Store.GetProject(r.Context(), p.OrgID, id)
	default:
		return httpx.ErrNotFound
	}
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return httpx.ErrNotFound
		}
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pr)
	return nil
}

func (s *Server) internalAgentVersion(w http.ResponseWriter, r *http.Request) error {
	p := principal(r)
	v, err := s.App.Store.GetAgentVersionByID(r.Context(), s.App.Store.Pool, p.OrgID, r.PathValue("id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return httpx.ErrNotFound
		}
		return err
	}
	tools, err := s.App.Store.ListVersionTools(r.Context(), v.ID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, app.VersionDetail{AgentVersion: v, Tools: tools})
	return nil
}

// internalAgentVersionByName resolves ?project_id=&agent=&version= to a
// version with its tools (the simulation service validates a run request
// and pins the manifest under test with it).
func (s *Server) internalAgentVersionByName(w http.ResponseWriter, r *http.Request) error {
	p := principal(r)
	q := r.URL.Query()
	projectID, agent, version := q.Get("project_id"), q.Get("agent"), q.Get("version")
	if !ids.Valid(projectID) || agent == "" || version == "" || len(agent) > 200 || len(version) > 100 {
		return httpx.Invalid("INVALID_QUERY", "project_id (UUID), agent and version are required.", nil)
	}
	if !p.CanAccessProject(projectID) {
		return httpx.ErrNotFound
	}
	v, err := s.App.Store.GetAgentVersionByName(r.Context(), s.App.Store.Pool, p.OrgID, projectID, agent, version)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return httpx.ErrNotFound
		}
		return err
	}
	tools, err := s.App.Store.ListVersionTools(r.Context(), v.ID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, app.VersionDetail{AgentVersion: v, Tools: tools})
	return nil
}
