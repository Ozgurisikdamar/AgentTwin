// Package api serves the dependency graph: a bounded view of it, its
// components with the evidence for each relationship, manual mappings and
// the blast radius of a change.
package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/graph"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/ingest"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/migrations"
)

// Producer names this service in the events it writes.
const Producer = "graph-service"

// Server serves the graph API.
type Server struct {
	Store  *store.Store
	Tokens *authn.TokenService
	Log    *slog.Logger
	Now    func() time.Time
}

// Router is what routes are registered on (an http.ServeMux; a recorder in
// the test that holds them to the API contract).
type Router interface {
	Handle(pattern string, handler http.Handler)
	HandleFunc(pattern string, handler func(http.ResponseWriter, *http.Request))
}

// Routes mounts the API behind the internal token check.
func (s *Server) Routes(mux Router) {
	api := http.NewServeMux()
	s.APIRoutes(api)
	mux.Handle("/api/", httpx.Chain(api, authn.RequireInternal(s.Tokens, "graph-service")))
}

// APIRoutes registers the graph routes.
func (s *Server) APIRoutes(mux Router) {
	h := httpx.Handle
	mux.HandleFunc("GET /api/v1/graph", h(s.view))
	mux.HandleFunc("GET /api/v1/graph/components", h(s.components))
	mux.HandleFunc("GET /api/v1/graph/components/{component_id}", h(s.component))
	mux.HandleFunc("PUT /api/v1/graph/mappings/{tool}", h(s.putMapping))
	mux.HandleFunc("POST /api/v1/blast-radius", h(s.blastRadius))
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// projectOf reads and checks the required project_id.
func projectOf(r *http.Request, raw string, perm authn.Permission) (authn.Principal, store.Scope, error) {
	id := strings.ToLower(raw)
	if id == "" {
		return authn.Principal{}, store.Scope{}, httpx.Invalid("INVALID_PARAMETER", "project_id is required.", map[string]any{"field": "project_id"})
	}
	if !ids.Valid(id) {
		return authn.Principal{}, store.Scope{}, httpx.Invalid("INVALID_PARAMETER", "project_id must be a UUID.", map[string]any{"field": "project_id"})
	}
	p, err := authn.RequireProject(r, perm, id)
	if err != nil {
		return p, store.Scope{}, err
	}
	return p, store.Scope{OrgID: p.OrgID, ProjectID: id}, nil
}

// ---------------------------------------------------------------- JSON shapes

type nodeJSON struct {
	ID         string         `json:"id"`
	Kind       graph.Kind     `json:"kind"`
	Key        string         `json:"key"`
	Label      string         `json:"label"`
	Attributes map[string]any `json:"attributes"`
}

func nodeOf(n graph.Node) nodeJSON {
	attrs := n.Attrs
	if attrs == nil {
		attrs = map[string]any{}
	}
	return nodeJSON{ID: n.ID, Kind: n.Kind, Key: n.Key, Label: n.Label, Attributes: attrs}
}

func nodesOf(ns []graph.Node) []nodeJSON {
	out := make([]nodeJSON, len(ns))
	for i, n := range ns {
		out[i] = nodeOf(n)
	}
	return out
}

type edgeJSON struct {
	ID         string         `json:"id"`
	From       string         `json:"from"`
	To         string         `json:"to"`
	Type       graph.EdgeType `json:"type"`
	Confidence float64        `json:"confidence"`
	Sources    []graph.Source `json:"sources"`
	Certain    bool           `json:"certain"`
}

// ---------------------------------------------------------------- view

var kindsParam = regexp.MustCompile(`^[A-Z_]+(,[A-Z_]+)*$`)

func (s *Server) view(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	_, sc, err := projectOf(r, q.Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	depth, err := httpx.QueryInt(r, "depth", graph.DefaultViewDepth, 1, graph.MaxViewDepth)
	if err != nil {
		return err
	}
	limit, err := httpx.QueryInt(r, "limit", graph.DefaultViewLimit, 1, graph.MaxViewLimit)
	if err != nil {
		return err
	}
	var kinds []graph.Kind
	if raw := q.Get("kinds"); raw != "" {
		if !kindsParam.MatchString(raw) {
			return httpx.Invalid("INVALID_PARAMETER", "kinds is a comma-separated list of component kinds.", map[string]any{"field": "kinds"})
		}
		for _, k := range strings.Split(raw, ",") {
			if !graph.Kind(k).Valid() {
				return httpx.Invalid("INVALID_PARAMETER", fmt.Sprintf("Unknown component kind %q.", k), map[string]any{"field": "kinds"})
			}
			kinds = append(kinds, graph.Kind(k))
		}
	}
	reader := s.Store.Reader(sc)
	var focus []graph.Node
	if f := strings.ToLower(q.Get("focus")); f != "" {
		if !ids.Valid(f) {
			return httpx.Invalid("INVALID_PARAMETER", "focus must be a component id.", map[string]any{"field": "focus"})
		}
		found, err := reader.Nodes(r.Context(), []string{f})
		if err != nil {
			return err
		}
		n, ok := found[f]
		if !ok {
			return httpx.ErrNotFound
		}
		focus = []graph.Node{n}
	} else if focus, err = s.Store.LatestVersions(r.Context(), sc); err != nil {
		// No focus: every agent's latest version, the relevant part of a
		// project's graph (spec §113).
		return err
	}
	v, err := graph.Neighborhood(r.Context(), reader, focus, depth, limit, kinds)
	if err != nil {
		return err
	}
	counts, edgeCount, err := s.Store.Counts(r.Context(), sc)
	if err != nil {
		return err
	}
	edges := make([]edgeJSON, len(v.Edges))
	for i, e := range v.Edges {
		edges[i] = edgeJSON{ID: e.ID, From: e.From, To: e.To, Type: e.Type, Confidence: e.Confidence, Sources: e.Sources, Certain: e.Certain()}
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{
		"focus": nodesOf(v.Focus), "nodes": nodesOf(v.Nodes), "edges": edges,
		"depth": v.Depth, "truncated": v.Truncated,
		"totals": map[string]any{"components": counts, "edges": edgeCount},
	})
	return nil
}

// ---------------------------------------------------------------- components

type listCursor struct {
	Kind string `json:"k"`
	Key  string `json:"key"`
}

func (s *Server) components(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	_, sc, err := projectOf(r, q.Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	f := store.ListFilter{Query: strings.TrimSpace(q.Get("q"))}
	if k := q.Get("kind"); k != "" {
		if !graph.Kind(k).Valid() {
			return httpx.Invalid("INVALID_PARAMETER", fmt.Sprintf("Unknown component kind %q.", k), map[string]any{"field": "kind"})
		}
		f.Kind = graph.Kind(k)
	}
	if len([]rune(f.Query)) > 200 {
		return httpx.Invalid("INVALID_PARAMETER", "q is at most 200 characters.", map[string]any{"field": "q"})
	}
	if f.Limit, err = httpx.QueryInt(r, "limit", 50, 1, 200); err != nil {
		return err
	}
	if c := q.Get("cursor"); c != "" {
		var cur listCursor
		raw, derr := base64.RawURLEncoding.DecodeString(c)
		if len(c) > 1024 || derr != nil || json.Unmarshal(raw, &cur) != nil || !graph.Kind(cur.Kind).Valid() || cur.Key == "" {
			return httpx.Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
		}
		f.AfterKind, f.AfterKey = cur.Kind, cur.Key
	}
	want := f.Limit
	f.Limit++
	rows, err := s.Store.Components(r.Context(), sc, f)
	if err != nil {
		return err
	}
	var next any
	if len(rows) > want {
		rows = rows[:want]
		last := rows[len(rows)-1]
		b, _ := json.Marshal(listCursor{Kind: string(last.Kind), Key: last.Key})
		next = base64.RawURLEncoding.EncodeToString(b)
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": nodesOf(rows), "next_cursor": next})
	return nil
}

const relationLimit = 200

func (s *Server) component(w http.ResponseWriter, r *http.Request) error {
	p, err := authn.Require(r, authn.PermRead)
	if err != nil {
		return err
	}
	id := strings.ToLower(r.PathValue("component_id"))
	if !ids.Valid(id) {
		return httpx.Invalid("INVALID_PARAMETER", "component_id must be a UUID.", map[string]any{"field": "component_id"})
	}
	c, err := s.Store.Component(r.Context(), p.OrgID, id)
	if errors.Is(err, store.ErrNotFound) || (err == nil && !p.CanAccessProject(c.ProjectID)) {
		return httpx.ErrNotFound
	}
	if err != nil {
		return err
	}
	rels, truncated, err := s.Store.Relations(r.Context(), c, relationLimit)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{
		"component": map[string]any{
			"id": c.ID, "project_id": c.ProjectID, "kind": c.Kind, "key": c.Key, "label": c.Label,
			"attributes": nodeOf(c.Node).Attributes, "first_seen_at": c.FirstSeenAt, "last_seen_at": c.LastSeenAt,
		},
		"relations": rels, "truncated": truncated,
	})
	return nil
}

// ---------------------------------------------------------------- mappings

var toolName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

type mappingBody struct {
	ProjectID string `json:"project_id"`
	ingest.Mapping
}

func (s *Server) putMapping(w http.ResponseWriter, r *http.Request) error {
	tool := r.PathValue("tool")
	if !toolName.MatchString(tool) {
		return httpx.Invalid("INVALID_PARAMETER", "tool must be a tool name.", map[string]any{"field": "tool"})
	}
	var body mappingBody
	if err := httpx.DecodeJSON(w, r, &body, 64<<10); err != nil {
		return err
	}
	p, sc, err := projectOf(r, body.ProjectID, authn.PermGraphWrite)
	if err != nil {
		return err
	}
	if len(body.DependsOn) > 50 {
		return httpx.Invalid("INVALID_MAPPING", "A tool maps to at most 50 dependencies.", map[string]any{"field": "depends_on"})
	}
	if body.DependsOn == nil {
		return httpx.Invalid("INVALID_MAPPING", "depends_on is required (an empty list removes the mapping).", map[string]any{"field": "depends_on"})
	}
	for i, d := range body.DependsOn {
		if d.Name == "" || len([]rune(d.Name)) > 200 {
			return httpx.Invalid("INVALID_MAPPING", "A dependency name is 1 to 200 characters.", map[string]any{"field": fmt.Sprintf("depends_on[%d].name", i)})
		}
	}
	facts, err := ingest.FromMapping(tool, body.Mapping)
	if err != nil {
		return httpx.Invalid("INVALID_MAPPING", err.Error(), map[string]any{"field": "depends_on"})
	}
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		if err := s.Store.Apply(r.Context(), tx, sc, facts, s.now()); err != nil {
			return err
		}
		return emitAudit(r.Context(), tx, p, sc.ProjectID, "tool_mapping.set", "tool", tool, len(body.DependsOn))
	})
	if err != nil {
		return err
	}
	found, err := s.Store.Reader(sc).Lookup(r.Context(), []graph.Ref{{Kind: graph.KindTool, Key: tool}})
	if err != nil {
		return err
	}
	c, err := s.Store.Component(r.Context(), p.OrgID, found[graph.Ref{Kind: graph.KindTool, Key: tool}].ID)
	if err != nil {
		return err
	}
	rels, _, err := s.Store.Relations(r.Context(), c, relationLimit)
	if err != nil {
		return err
	}
	mapped := []store.Relation{}
	for _, rel := range rels {
		for _, ev := range rel.Evidence {
			if ev.Source == graph.SourceManual && ev.SourceRef == "mapping:"+tool {
				mapped = append(mapped, rel)
				break
			}
		}
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"tool": nodeOf(c.Node), "mapped": mapped})
	return nil
}

func emitAudit(ctx context.Context, tx pgx.Tx, p authn.Principal, projectID, action, resourceType, resourceID string, count int) error {
	var rid any
	if v := logx.RequestID(ctx); v != "" {
		rid = v
	}
	env, err := events.New("audit.recorded.v1", Producer, p.OrgID, projectID, logx.RequestID(ctx), "", map[string]any{
		"actor": p.Actor, "action": action, "resource_type": resourceType, "resource_id": resourceID,
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "request_id": rid, "reason": nil,
		"metadata": map[string]any{"dependencies": count},
	})
	if err != nil {
		return fmt.Errorf("audit event: %w", err)
	}
	return events.WriteOutbox(ctx, tx, migrations.Schema, env)
}

// ---------------------------------------------------------------- blast radius

type blastBody struct {
	ProjectID string         `json:"project_id"`
	Changes   []graph.Change `json:"changes"`
	Scope     *graph.Scope   `json:"scope"`
	MaxDepth  *int           `json:"max_depth"`
	MaxNodes  *int           `json:"max_nodes"`
}

// Blast radius limits.
const (
	MaxChanges  = 100
	MaxMentions = 100
	MaxNodes    = 2000
)

func (s *Server) blastRadius(w http.ResponseWriter, r *http.Request) error {
	var body blastBody
	if err := httpx.DecodeJSON(w, r, &body, 256<<10); err != nil {
		return err
	}
	_, sc, err := projectOf(r, body.ProjectID, authn.PermRead)
	if err != nil {
		return err
	}
	if len(body.Changes) == 0 || len(body.Changes) > MaxChanges {
		return httpx.Invalid("INVALID_CHANGES", fmt.Sprintf("changes lists 1 to %d changed components.", MaxChanges), map[string]any{"field": "changes"})
	}
	for i, c := range body.Changes {
		field := fmt.Sprintf("changes[%d]", i)
		if err := c.Ref.Validate(); err != nil {
			return httpx.Invalid("INVALID_CHANGES", err.Error(), map[string]any{"field": field + ".component"})
		}
		if !slices.Contains(graph.ChangeKinds, c.Change) {
			return httpx.Invalid("INVALID_CHANGES", "A change is added, removed or modified.", map[string]any{"field": field + ".change"})
		}
		if len(c.Mentions) > MaxMentions || len([]rune(c.Summary)) > 300 {
			return httpx.Invalid("INVALID_CHANGES", fmt.Sprintf("A change has a summary of at most 300 characters and at most %d mentions.", MaxMentions), map[string]any{"field": field})
		}
		for _, m := range c.Mentions {
			if !toolName.MatchString(m) {
				return httpx.Invalid("INVALID_CHANGES", "A mention is a tool name.", map[string]any{"field": field + ".mentions"})
			}
		}
	}
	if body.Scope != nil && (body.Scope.Agent == "" || body.Scope.Version == "" || len(body.Scope.Agent) > 63 || len(body.Scope.Version) > 100) {
		return httpx.Invalid("INVALID_SCOPE", "scope names an agent and one of its versions.", map[string]any{"field": "scope"})
	}
	opts := graph.Options{Scope: body.Scope}
	if body.MaxDepth != nil {
		if *body.MaxDepth < 1 || *body.MaxDepth > graph.MaxMaxDepth {
			return httpx.Invalid("INVALID_PARAMETER", fmt.Sprintf("max_depth is 1 to %d.", graph.MaxMaxDepth), map[string]any{"field": "max_depth"})
		}
		opts.MaxDepth = *body.MaxDepth
	}
	if body.MaxNodes != nil {
		if *body.MaxNodes < 1 || *body.MaxNodes > MaxNodes {
			return httpx.Invalid("INVALID_PARAMETER", fmt.Sprintf("max_nodes is 1 to %d.", MaxNodes), map[string]any{"field": "max_nodes"})
		}
		opts.MaxNodes = *body.MaxNodes
	}
	res, err := graph.BlastRadius(r.Context(), s.Store.Reader(sc), body.Changes, opts)
	if err != nil {
		// The changes were validated above: what remains is the store's.
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"blast_radius": res})
	return nil
}

// ---------------------------------------------------------------- events

// Handlers are the consumer's handlers, one per event type.
func (s *Server) Handlers() map[string]events.Handler {
	on := func(mapFacts func(events.Envelope) (ingest.Facts, error)) events.Handler {
		return func(ctx context.Context, env events.Envelope) error {
			if env.ProjectID == nil || !ids.Valid(*env.ProjectID) || !ids.Valid(env.OrganizationID) {
				s.Log.WarnContext(ctx, "event without a project; not graphed", "type", env.Type, "event_id", env.ID)
				return nil
			}
			f, err := mapFacts(env)
			if err != nil {
				// A payload that does not map never will: park it, do not retry.
				return events.Permanent(err)
			}
			if len(f.Nodes) == 0 && len(f.Edges) == 0 {
				return nil
			}
			err = s.Store.ApplyEvent(ctx, env.ID, store.Scope{OrgID: env.OrganizationID, ProjectID: *env.ProjectID}, f, env.OccurredAt)
			if errors.Is(err, store.ErrStaleDeclaration) {
				s.Log.InfoContext(ctx, "an older declaration arrived after a newer one; ignored",
					"type", env.Type, "event_id", env.ID, "occurred_at", env.OccurredAt)
				return nil
			}
			return err
		}
	}
	return map[string]events.Handler{
		"agent.version_registered.v1": on(func(e events.Envelope) (ingest.Facts, error) {
			return ingest.FromAgentVersion(e.Payload, e.OccurredAt)
		}),
		"tool.catalog_imported.v1": on(func(e events.Envelope) (ingest.Facts, error) { return ingest.FromToolCatalog(e.Payload) }),
		"trace.ingested.v1":        on(func(e events.Envelope) (ingest.Facts, error) { return ingest.FromTrace(e.Payload) }),
		"scenario.upserted.v1":     on(func(e events.Envelope) (ingest.Facts, error) { return ingest.FromScenario(e.Payload) }),
		"policy.activated.v1":      on(func(e events.Envelope) (ingest.Facts, error) { return ingest.FromPolicy(e.Payload) }),
	}
}
