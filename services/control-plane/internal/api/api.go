// Package api is the control-plane HTTP layer: routing, request decoding and
// response rendering. Use cases live in package app.
package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
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

// Router is what routes are registered on: an *http.ServeMux, or a recorder
// in the test that holds the routes to the API contract.
type Router interface {
	Handle(pattern string, handler http.Handler)
	HandleFunc(pattern string, handler func(http.ResponseWriter, *http.Request))
}

// Server holds dependencies for handlers.
type Server struct {
	App      *app.App
	Auth     *auth.Authenticator
	Tokens   *authn.TokenService
	Proxy    *proxy.Proxy
	Log      *slog.Logger
	Extra    func(mux Router) // later phases register more routes
	AuthMode auth.Mode
	OIDC     map[string]string
}

func principal(r *http.Request) authn.Principal {
	p, _ := authn.FromContext(r.Context())
	return p
}

// pathID returns the path value name, which must be a UUID: a malformed id
// answers 400 INVALID_PARAMETER instead of reaching the database.
func pathID(r *http.Request, name, field string) (string, error) {
	v := r.PathValue(name)
	if !ids.Valid(v) {
		return "", httpx.Invalid("INVALID_PARAMETER", field+" must be a UUID.", map[string]any{"field": field})
	}
	return strings.ToLower(v), nil
}

// decodeOptionalJSON decodes a JSON body when there is one; an absent or
// empty body leaves dst as it is.
func decodeOptionalJSON(w http.ResponseWriter, r *http.Request, dst any, maxBytes int64) error {
	body, err := httpx.ReadLimited(w, r, maxBytes)
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return nil
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	return httpx.DecodeJSON(w, r, dst, maxBytes)
}

// PublicRoutes registers the public API on mux (behind authentication).
func (s *Server) PublicRoutes(mux Router) {
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

	mux.HandleFunc("POST /api/v1/projects/{id}/imports/openapi", h(s.importOpenAPI))
	mux.HandleFunc("POST /api/v1/projects/{id}/imports/mcp", h(s.importMCP))
	mux.HandleFunc("GET /api/v1/projects/{id}/tool-catalogs", h(s.listCatalogs))
	mux.HandleFunc("GET /api/v1/tool-catalogs/{id}", h(s.getCatalog))

	mux.HandleFunc("POST /api/v1/projects/{id}/change-sets", h(s.createChangeSet))
	mux.HandleFunc("GET /api/v1/projects/{id}/change-sets", h(s.listChangeSets))
	mux.HandleFunc("GET /api/v1/change-sets/{id}", h(s.getChangeSet))
	mux.HandleFunc("GET /api/v1/change-sets/{id}/impact", h(s.changeSetImpact))

	mux.HandleFunc("POST /api/v1/releases", h(s.createRelease))
	mux.HandleFunc("GET /api/v1/releases", h(s.listReleases))
	mux.HandleFunc("GET /api/v1/releases/{id}", h(s.getRelease))
	mux.HandleFunc("POST /api/v1/releases/{id}/evaluate", h(s.evaluateRelease))
	mux.HandleFunc("GET /api/v1/releases/{id}/gate", h(s.releaseGate))
	mux.HandleFunc("POST /api/v1/releases/{id}/override", h(s.overrideGate))

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
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	pr, err := s.App.GetProject(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pr)
	return nil
}

func (s *Server) updateProject(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in store.ProjectSettings
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	pr, err := s.App.UpdateProjectSettings(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pr)
	return nil
}

func (s *Server) listEnvironments(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	es, err := s.App.ListEnvironments(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": es})
	return nil
}

func (s *Server) createAPIKey(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.CreateAPIKeyInput
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	k, err := s.App.CreateAPIKey(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, k)
	return nil
}

func (s *Server) listAPIKeys(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	ks, err := s.App.ListAPIKeys(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ks})
	return nil
}

func (s *Server) revokeAPIKey(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "key_id")
	if err != nil {
		return err
	}
	var in struct {
		Reason string `json:"reason"`
	}
	if err := decodeOptionalJSON(w, r, &in, 4096); err != nil {
		return err
	}
	k, err := s.App.RevokeAPIKey(r.Context(), principal(r), id, in.Reason)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, k)
	return nil
}

func (s *Server) rotateAPIKey(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "key_id")
	if err != nil {
		return err
	}
	k, err := s.App.RotateAPIKey(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, k)
	return nil
}

func (s *Server) createAgent(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.CreateAgentInput
	if err := httpx.DecodeJSON(w, r, &in, 0); err != nil {
		return err
	}
	a, err := s.App.CreateAgent(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, a)
	return nil
}

func (s *Server) listAgents(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	as, err := s.App.ListAgents(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": as})
	return nil
}

// readManifestRequest accepts either a raw YAML/JSON manifest (commit metadata
// in query parameters) or a JSON envelope {"manifest": ..., "commit_sha": ...}.
// An envelope is strict like every JSON body: unknown fields are rejected.
func readManifestRequest(w http.ResponseWriter, r *http.Request) ([]byte, app.VersionMetadata, error) {
	body, err := httpx.ReadLimited(w, r, domain.MaxManifestBytes+4096)
	if err != nil {
		return nil, app.VersionMetadata{}, err
	}
	meta := app.VersionMetadata{CommitSHA: r.URL.Query().Get("commit_sha"), RepoURL: r.URL.Query().Get("repo_url"), Branch: r.URL.Query().Get("branch")}
	ct := strings.ToLower(r.Header.Get("Content-Type"))
	var probe map[string]json.RawMessage
	if strings.HasPrefix(ct, "application/json") && json.Unmarshal(body, &probe) == nil && probe["manifest"] != nil {
		var env struct {
			Manifest  json.RawMessage `json:"manifest"`
			CommitSHA string          `json:"commit_sha"`
			RepoURL   string          `json:"repo_url"`
			Branch    string          `json:"branch"`
		}
		dec := json.NewDecoder(bytes.NewReader(body))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&env); err != nil {
			return nil, meta, httpx.ErrInvalidJSON.WithDetails(map[string]any{"reason": err.Error()})
		}
		if len(env.Manifest) > 0 {
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
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	raw, meta, err := readManifestRequest(w, r)
	if err != nil {
		return err
	}
	res, err := s.App.RegisterManifest(r.Context(), principal(r), id, raw, meta)
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
	id, err := pathID(r, "id", "agent_id")
	if err != nil {
		return err
	}
	a, err := s.App.GetAgent(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, a)
	return nil
}

func (s *Server) createVersion(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "agent_id")
	if err != nil {
		return err
	}
	agent, err := s.App.GetAgent(r.Context(), principal(r), id)
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
	id, err := pathID(r, "id", "agent_id")
	if err != nil {
		return err
	}
	vs, err := s.App.ListVersions(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": vs})
	return nil
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "agent_id")
	if err != nil {
		return err
	}
	v, err := s.App.GetVersion(r.Context(), principal(r), id, r.PathValue("version"))
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, v)
	return nil
}

func (s *Server) listTools(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	ts, err := s.App.ListTools(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": ts})
	return nil
}

func (s *Server) createTool(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.CreateToolInput
	if err := httpx.DecodeJSON(w, r, &in, 256<<10); err != nil {
		return err
	}
	res, err := s.App.CreateTool(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	// As for manifests: 201 when a version was stored, 200 when the
	// definition equals the latest version.
	status := http.StatusCreated
	if created, _ := res["created"].(bool); !created {
		status = http.StatusOK
	}
	httpx.WriteJSON(w, status, res)
	return nil
}

// Bounds of an import request: an OpenAPI document of up to 2 MiB as text
// (escaped in JSON it grows), a tools/list result of up to 1000 tools.
const (
	maxOpenAPIImportBody = 6 << 20
	maxMCPImportBody     = 4 << 20
)

// importStatus is 201 when a catalog revision was stored, 200 when the
// import equals the latest revision.
func importStatus(c app.ImportedCatalog) int {
	if c.Created {
		return http.StatusCreated
	}
	return http.StatusOK
}

func (s *Server) importOpenAPI(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.ImportOpenAPIInput
	if err := httpx.DecodeJSON(w, r, &in, maxOpenAPIImportBody); err != nil {
		return err
	}
	c, err := s.App.ImportOpenAPI(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, importStatus(c), c)
	return nil
}

func (s *Server) importMCP(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.ImportMCPInput
	if err := httpx.DecodeJSON(w, r, &in, maxMCPImportBody); err != nil {
		return err
	}
	c, err := s.App.ImportMCP(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, importStatus(c), c)
	return nil
}

func (s *Server) listCatalogs(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	limit, err := httpx.QueryInt(r, "limit", 50, 1, 200)
	if err != nil {
		return err
	}
	cur, err := httpx.DecodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		return err
	}
	if cur != nil && !ids.Valid(cur.ID) {
		return httpx.Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	q := r.URL.Query()
	items, err := s.App.ListCatalogs(r.Context(), principal(r), id, app.CatalogPage{
		Source: q.Get("source"), Name: q.Get("name"), Before: cur, Limit: limit + 1,
	})
	if err != nil {
		return err
	}
	var next *string // null on the last page
	if len(items) > limit {
		items = items[:limit]
		last := items[len(items)-1]
		c := httpx.EncodeCursor(httpx.Cursor{TS: last.CreatedAt, ID: last.ID})
		next = &c
	}
	if items == nil {
		items = []store.CatalogSummary{}
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items, "next_cursor": next})
	return nil
}

func (s *Server) getCatalog(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "catalog_id")
	if err != nil {
		return err
	}
	c, err := s.App.GetCatalog(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, c)
	return nil
}

// maxChangeSetBody bounds a change set request: up to 1000 changed file
// names of at most 512 bytes and 50 declared changes.
const maxChangeSetBody = 1 << 20

func (s *Server) createChangeSet(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var in app.CreateChangeSetInput
	if err := httpx.DecodeJSON(w, r, &in, maxChangeSetBody); err != nil {
		return err
	}
	cs, err := s.App.CreateChangeSet(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	// As for manifests: 201 when stored, 200 when the same change set was
	// stored before.
	status := http.StatusCreated
	if !cs.Created {
		status = http.StatusOK
	}
	httpx.WriteJSON(w, status, cs)
	return nil
}

func (s *Server) listChangeSets(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	limit, err := httpx.QueryInt(r, "limit", 50, 1, 200)
	if err != nil {
		return err
	}
	cur, err := httpx.DecodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		return err
	}
	if cur != nil && !ids.Valid(cur.ID) {
		return httpx.Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	items, err := s.App.ListChangeSets(r.Context(), principal(r), id, app.ChangeSetPage{
		Agent: r.URL.Query().Get("agent"), Before: cur, Limit: limit + 1,
	})
	if err != nil {
		return err
	}
	var next *string // null on the last page
	if len(items) > limit {
		items = items[:limit]
		last := items[len(items)-1]
		c := httpx.EncodeCursor(httpx.Cursor{TS: last.CreatedAt, ID: last.ID})
		next = &c
	}
	if items == nil {
		items = []store.ChangeSetSummary{}
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items, "next_cursor": next})
	return nil
}

func (s *Server) getChangeSet(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "change_set_id")
	if err != nil {
		return err
	}
	cs, err := s.App.GetChangeSet(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, cs)
	return nil
}

func (s *Server) changeSetImpact(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "change_set_id")
	if err != nil {
		return err
	}
	imp, err := s.App.ChangeSetImpact(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, imp)
	return nil
}

// maxOverrideBody bounds an override request: a reason of at most 2000
// characters and a ticket URL of at most 2048.
const maxOverrideBody = 64 << 10

func (s *Server) createRelease(w http.ResponseWriter, r *http.Request) error {
	var in app.CreateReleaseInput
	if err := httpx.DecodeJSON(w, r, &in, maxChangeSetBody); err != nil {
		return err
	}
	rel, err := s.App.CreateRelease(r.Context(), principal(r), in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, rel)
	return nil
}

func (s *Server) listReleases(w http.ResponseWriter, r *http.Request) error {
	limit, err := httpx.QueryInt(r, "limit", 50, 1, 200)
	if err != nil {
		return err
	}
	cur, err := httpx.DecodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		return err
	}
	if cur != nil && !ids.Valid(cur.ID) {
		return httpx.Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	q := r.URL.Query()
	items, err := s.App.ListReleases(r.Context(), principal(r), strings.ToLower(q.Get("project_id")), app.ReleasePage{
		Agent: q.Get("agent"), Before: cur, Limit: limit + 1,
	})
	if err != nil {
		return err
	}
	var next *string // null on the last page
	if len(items) > limit {
		items = items[:limit]
		last := items[len(items)-1]
		c := httpx.EncodeCursor(httpx.Cursor{TS: last.CreatedAt, ID: last.ID})
		next = &c
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items, "next_cursor": next})
	return nil
}

func (s *Server) getRelease(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "release_id")
	if err != nil {
		return err
	}
	rel, err := s.App.GetRelease(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, rel)
	return nil
}

func (s *Server) evaluateRelease(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "release_id")
	if err != nil {
		return err
	}
	g, err := s.App.EvaluateRelease(r.Context(), principal(r), id)
	if err != nil {
		return err
	}
	// 202 while the evaluation service runs the suite; a suite with nothing
	// to run is decided at once.
	status := http.StatusAccepted
	if g.Status != "EVALUATING" {
		status = http.StatusCreated
	}
	httpx.WriteJSON(w, status, g)
	return nil
}

func (s *Server) releaseGate(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "release_id")
	if err != nil {
		return err
	}
	revision, err := httpx.QueryInt(r, "revision", 0, 1, 1_000_000)
	if err != nil {
		return err
	}
	g, err := s.App.ReleaseGate(r.Context(), principal(r), id, revision)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, g)
	return nil
}

func (s *Server) overrideGate(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "id", "release_id")
	if err != nil {
		return err
	}
	var in app.OverrideInput
	if err := httpx.DecodeJSON(w, r, &in, maxOverrideBody); err != nil {
		return err
	}
	g, err := s.App.OverrideGate(r.Context(), principal(r), id, in)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, g)
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
	var next *string // null on the last page
	if len(items) == limit {
		c := strconv.FormatInt(items[len(items)-1].Seq, 10)
		next = &c
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
func (s *Server) InternalRoutes(mux Router) {
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
	id, err := pathID(r, "id", "project_id")
	if err != nil {
		return err
	}
	var pr store.Project
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
	id, err := pathID(r, "id", "version_id")
	if err != nil {
		return err
	}
	v, err := s.App.Store.GetAgentVersionByID(r.Context(), s.App.Store.Pool, p.OrgID, id)
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
