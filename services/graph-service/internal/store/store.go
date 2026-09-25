// Package store persists the dependency graph in the graph schema and reads
// it for traversals.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/graph"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/ingest"
)

// ErrNotFound is returned for a component outside the caller's scope.
var ErrNotFound = errors.New("not found")

// Store wraps the pool.
type Store struct{ Pool *pgxpool.Pool }

// New creates a store.
func New(pool *pgxpool.Pool) *Store { return &Store{Pool: pool} }

// Scope is one project of one organization: every query names both.
type Scope struct {
	OrgID     string
	ProjectID string
}

// Apply writes facts: components (attributes merged), relationships and
// their evidence, then the replacements the facts declare, then the
// derived sources and confidence of every edge it touched.
func (s *Store) Apply(ctx context.Context, tx pgx.Tx, sc Scope, f ingest.Facts, at time.Time) error {
	if err := f.Validate(); err != nil {
		return err
	}
	// Writes to one project's graph are serialized: concurrent consumers
	// would otherwise lock the same components in different orders
	// (deadlocks) and could each see the other's agent version as absent
	// when recomputing the latest one. Graph writes are small and rare next
	// to reads, which take no lock.
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended('graph:' || $1::text || ':' || $2::text, 0))`, sc.OrgID, sc.ProjectID); err != nil {
		return fmt.Errorf("lock project graph: %w", err)
	}
	nodes := map[graph.Ref]ingest.NodeFact{}
	var order []graph.Ref
	add := func(n ingest.NodeFact) {
		cur, ok := nodes[n.Ref]
		if !ok {
			order = append(order, n.Ref)
			nodes[n.Ref] = ingest.NodeFact{Ref: n.Ref, Label: n.Label, Attrs: maps(n.Attrs)}
			return
		}
		for k, v := range n.Attrs {
			cur.Attrs[k] = v
		}
		if n.Label != n.Ref.Key {
			cur.Label = n.Label
		}
		nodes[n.Ref] = cur
	}
	for _, n := range f.Nodes {
		add(n)
	}
	for _, e := range f.Edges { // an endpoint no fact describes is still a component
		for _, r := range []graph.Ref{e.From, e.To} {
			if _, ok := nodes[r]; !ok {
				add(ingest.NodeFact{Ref: r, Label: r.Key, Attrs: map[string]any{}})
			}
		}
	}
	slices.SortFunc(order, func(a, b graph.Ref) int { return strings.Compare(a.String(), b.String()) })
	idOf := map[graph.Ref]string{}
	for _, r := range order {
		n := nodes[r]
		attrs, err := json.Marshal(n.Attrs)
		if err != nil {
			return err
		}
		var id string
		err = tx.QueryRow(ctx, `
			INSERT INTO graph.component (id, organization_id, project_id, kind, key, label, attributes, first_seen_at, last_seen_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
			ON CONFLICT (organization_id, project_id, kind, key) DO UPDATE SET
				label = CASE WHEN EXCLUDED.label <> EXCLUDED.key THEN EXCLUDED.label ELSE graph.component.label END,
				attributes = graph.component.attributes || EXCLUDED.attributes,
				last_seen_at = GREATEST(graph.component.last_seen_at, EXCLUDED.last_seen_at)
			RETURNING id`,
			ids.New(), sc.OrgID, sc.ProjectID, string(r.Kind), r.Key, truncate(n.Label, 300), attrs, at).Scan(&id)
		if err != nil {
			return fmt.Errorf("component %s: %w", r, err)
		}
		idOf[r] = id
	}
	touched := map[string]bool{}
	asserted := map[ingest.Replace][]string{}
	for _, e := range f.Edges {
		var edgeID string
		err := tx.QueryRow(ctx, `
			INSERT INTO graph.dependency_edge (id, organization_id, project_id, from_id, to_id, type, first_seen_at, last_seen_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
			ON CONFLICT (from_id, type, to_id) DO UPDATE SET last_seen_at = GREATEST(graph.dependency_edge.last_seen_at, EXCLUDED.last_seen_at)
			RETURNING id`,
			ids.New(), sc.OrgID, sc.ProjectID, idOf[e.From], idOf[e.To], string(e.Type), at).Scan(&edgeID)
		if err != nil {
			return fmt.Errorf("edge %s -%s-> %s: %w", e.From, e.Type, e.To, err)
		}
		detail, err := json.Marshal(e.Detail)
		if err != nil {
			return err
		}
		// Observed evidence counts calls; declared evidence is re-asserted,
		// not counted again.
		_, err = tx.Exec(ctx, `
			INSERT INTO graph.edge_evidence (edge_id, source, source_ref, confidence, observations, detail, first_seen_at, last_seen_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
			ON CONFLICT (edge_id, source, source_ref) DO UPDATE SET
				observations = CASE WHEN EXCLUDED.source = 'OBSERVED'
					THEN graph.edge_evidence.observations + EXCLUDED.observations ELSE graph.edge_evidence.observations END,
				detail = EXCLUDED.detail,
				confidence = EXCLUDED.confidence,
				last_seen_at = GREATEST(graph.edge_evidence.last_seen_at, EXCLUDED.last_seen_at)`,
			edgeID, string(e.Source), truncate(e.SourceRef, 300), e.Source.Confidence(), max(e.Observations, 1), detail, at)
		if err != nil {
			return fmt.Errorf("evidence: %w", err)
		}
		touched[edgeID] = true
		key := ingest.Replace{Source: e.Source, SourceRef: e.SourceRef}
		asserted[key] = append(asserted[key], edgeID)
	}
	for _, rep := range f.Replaces {
		kept := asserted[rep]
		if kept == nil {
			kept = []string{}
		}
		rows, err := tx.Query(ctx, `
			DELETE FROM graph.edge_evidence v USING graph.dependency_edge e
			WHERE v.edge_id = e.id AND e.organization_id = $1 AND e.project_id = $2
			  AND v.source = $3 AND v.source_ref = $4 AND NOT (v.edge_id = ANY($5::uuid[]))
			RETURNING v.edge_id`, sc.OrgID, sc.ProjectID, string(rep.Source), rep.SourceRef, kept)
		if err != nil {
			return fmt.Errorf("replace %s %s: %w", rep.Source, rep.SourceRef, err)
		}
		dropped, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		for _, id := range dropped {
			touched[id] = true
		}
	}
	if len(touched) > 0 {
		edgeIDs := make([]string, 0, len(touched))
		for id := range touched {
			edgeIDs = append(edgeIDs, id)
		}
		if _, err := tx.Exec(ctx, `
			UPDATE graph.dependency_edge e SET sources = s.sources, confidence = s.confidence
			FROM (SELECT edge_id, array_agg(DISTINCT source ORDER BY source) AS sources, max(confidence) AS confidence
			      FROM graph.edge_evidence WHERE edge_id = ANY($1::uuid[]) GROUP BY edge_id) s
			WHERE e.id = s.edge_id`, edgeIDs); err != nil {
			return fmt.Errorf("derive sources: %w", err)
		}
		if _, err := tx.Exec(ctx, `
			DELETE FROM graph.dependency_edge e WHERE e.id = ANY($1::uuid[])
			  AND NOT EXISTS (SELECT 1 FROM graph.edge_evidence v WHERE v.edge_id = e.id)`, edgeIDs); err != nil {
			return fmt.Errorf("drop unsupported edges: %w", err)
		}
	}
	for _, agent := range f.LatestOf {
		if err := markLatest(ctx, tx, sc, agent); err != nil {
			return err
		}
	}
	return nil
}

// markLatest flags the most recently registered version of an agent as its
// latest; versions only seen in traces were never registered and never are.
func markLatest(ctx context.Context, tx pgx.Tx, sc Scope, agent string) error {
	_, err := tx.Exec(ctx, `
		WITH versions AS (
			SELECT id, key, (attributes->>'registered_at')::timestamptz AS registered_at
			FROM graph.component
			WHERE organization_id = $1 AND project_id = $2 AND kind = 'AGENT_VERSION' AND attributes->>'agent' = $3
		), latest AS (
			SELECT id FROM versions WHERE registered_at IS NOT NULL ORDER BY registered_at DESC, key DESC LIMIT 1
		)
		UPDATE graph.component c
		SET attributes = c.attributes || jsonb_build_object('latest', c.id IN (SELECT id FROM latest))
		WHERE c.id IN (SELECT id FROM versions)`, sc.OrgID, sc.ProjectID, agent)
	if err != nil {
		return fmt.Errorf("latest version of %s: %w", agent, err)
	}
	return nil
}

// ApplyEvent applies facts for one event exactly once.
func (s *Store) ApplyEvent(ctx context.Context, eventID string, sc Scope, f ingest.Facts, at time.Time) error {
	return pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		fresh, err := db.MarkProcessed(ctx, tx, "graph", "graph-service", eventID)
		if err != nil || !fresh {
			return err
		}
		return s.Apply(ctx, tx, sc, f, at)
	})
}

func maps(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// ---------------------------------------------------------------- reading

// Reader reads one project's graph for a traversal.
type Reader struct {
	Q  db.Querier
	Sc Scope
}

// Reader returns a reader of the project's graph.
func (s *Store) Reader(sc Scope) *Reader { return &Reader{Q: s.Pool, Sc: sc} }

const nodeCols = `id, kind, key, label, attributes`

func scanNode(r pgx.CollectableRow) (graph.Node, error) {
	var n graph.Node
	var kind string
	var attrs []byte
	if err := r.Scan(&n.ID, &kind, &n.Key, &n.Label, &attrs); err != nil {
		return n, err
	}
	n.Kind = graph.Kind(kind)
	if err := json.Unmarshal(attrs, &n.Attrs); err != nil {
		return n, err
	}
	return n, nil
}

// Lookup resolves references.
func (r *Reader) Lookup(ctx context.Context, refs []graph.Ref) (map[graph.Ref]graph.Node, error) {
	kinds, keys := make([]string, len(refs)), make([]string, len(refs))
	for i, ref := range refs {
		kinds[i], keys[i] = string(ref.Kind), ref.Key
	}
	rows, err := r.Q.Query(ctx, `SELECT `+nodeCols+` FROM graph.component
		WHERE organization_id = $1 AND project_id = $2
		  AND (kind, key) IN (SELECT * FROM unnest($3::text[], $4::text[]))`, r.Sc.OrgID, r.Sc.ProjectID, kinds, keys)
	if err != nil {
		return nil, err
	}
	nodes, err := pgx.CollectRows(rows, scanNode)
	if err != nil {
		return nil, err
	}
	out := make(map[graph.Ref]graph.Node, len(nodes))
	for _, n := range nodes {
		out[n.Ref()] = n
	}
	return out, nil
}

// Nodes loads nodes by id.
func (r *Reader) Nodes(ctx context.Context, idList []string) (map[string]graph.Node, error) {
	rows, err := r.Q.Query(ctx, `SELECT `+nodeCols+` FROM graph.component
		WHERE organization_id = $1 AND project_id = $2 AND id = ANY($3::uuid[])`, r.Sc.OrgID, r.Sc.ProjectID, idList)
	if err != nil {
		return nil, err
	}
	nodes, err := pgx.CollectRows(rows, scanNode)
	if err != nil {
		return nil, err
	}
	out := make(map[string]graph.Node, len(nodes))
	for _, n := range nodes {
		out[n.ID] = n
	}
	return out, nil
}

// Edges returns the edges of the given types leaving (out) or entering ids.
func (r *Reader) Edges(ctx context.Context, idList []string, out bool, types []graph.EdgeType) ([]graph.Edge, error) {
	end := "to_id"
	if out {
		end = "from_id"
	}
	ts := make([]string, len(types))
	for i, t := range types {
		ts[i] = string(t)
	}
	rows, err := r.Q.Query(ctx, `SELECT id, from_id, to_id, type, confidence::float8, sources FROM graph.dependency_edge
		WHERE organization_id = $1 AND project_id = $2 AND `+end+` = ANY($3::uuid[]) AND type = ANY($4::text[])
		ORDER BY id`, r.Sc.OrgID, r.Sc.ProjectID, idList, ts)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (graph.Edge, error) {
		var e graph.Edge
		var t string
		var sources []string
		if err := row.Scan(&e.ID, &e.From, &e.To, &t, &e.Confidence, &sources); err != nil {
			return e, err
		}
		e.Type = graph.EdgeType(t)
		for _, s := range sources {
			e.Sources = append(e.Sources, graph.Source(s))
		}
		return e, nil
	})
}

// ---------------------------------------------------------------- queries

// Counts returns the number of components per kind and of edges.
func (s *Store) Counts(ctx context.Context, sc Scope) (map[string]int, int, error) {
	rows, err := s.Pool.Query(ctx, `SELECT kind, count(*) FROM graph.component
		WHERE organization_id = $1 AND project_id = $2 GROUP BY kind`, sc.OrgID, sc.ProjectID)
	if err != nil {
		return nil, 0, err
	}
	counts := map[string]int{}
	for rows.Next() {
		var k string
		var n int
		if err := rows.Scan(&k, &n); err != nil {
			return nil, 0, err
		}
		counts[k] = n
	}
	if err := rows.Err(); err != nil {
		return nil, 0, err
	}
	var edges int
	err = s.Pool.QueryRow(ctx, `SELECT count(*) FROM graph.dependency_edge
		WHERE organization_id = $1 AND project_id = $2`, sc.OrgID, sc.ProjectID).Scan(&edges)
	return counts, edges, err
}

// LatestVersions returns the latest registered version of every agent.
func (s *Store) LatestVersions(ctx context.Context, sc Scope) ([]graph.Node, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+nodeCols+` FROM graph.component
		WHERE organization_id = $1 AND project_id = $2 AND kind = 'AGENT_VERSION' AND attributes->>'latest' = 'true'
		ORDER BY key`, sc.OrgID, sc.ProjectID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, scanNode)
}

// ListFilter selects components.
type ListFilter struct {
	Kind      graph.Kind
	Query     string
	AfterKind string
	AfterKey  string
	Limit     int
}

// Components lists components by kind and key, optionally matching q.
func (s *Store) Components(ctx context.Context, sc Scope, f ListFilter) ([]graph.Node, error) {
	where := []string{"organization_id = $1", "project_id = $2"}
	args := []any{sc.OrgID, sc.ProjectID}
	arg := func(v any) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}
	if f.Kind != "" {
		where = append(where, "kind = "+arg(string(f.Kind)))
	}
	if f.Query != "" {
		p := arg("%" + escapeLike(strings.ToLower(f.Query)) + "%")
		where = append(where, "(lower(key) LIKE "+p+" ESCAPE '\\' OR lower(label) LIKE "+p+" ESCAPE '\\')")
	}
	if f.AfterKind != "" {
		where = append(where, "(kind, key) > ("+arg(f.AfterKind)+", "+arg(f.AfterKey)+")")
	}
	q := `SELECT ` + nodeCols + ` FROM graph.component WHERE ` + strings.Join(where, " AND ") +
		` ORDER BY kind, key LIMIT ` + arg(f.Limit)
	rows, err := s.Pool.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, scanNode)
}

func escapeLike(s string) string {
	return strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(s)
}

// Component is a component with where it was seen.
type Component struct {
	graph.Node
	ProjectID   string
	FirstSeenAt time.Time
	LastSeenAt  time.Time
}

// Component loads one component of the organization by id.
func (s *Store) Component(ctx context.Context, orgID, id string) (Component, error) {
	var c Component
	var kind string
	var attrs []byte
	err := s.Pool.QueryRow(ctx, `SELECT id, project_id, kind, key, label, attributes, first_seen_at, last_seen_at
		FROM graph.component WHERE organization_id = $1 AND id = $2`, orgID, id).
		Scan(&c.ID, &c.ProjectID, &kind, &c.Key, &c.Label, &attrs, &c.FirstSeenAt, &c.LastSeenAt)
	if db.IsNoRows(err) {
		return c, ErrNotFound
	}
	if err != nil {
		return c, err
	}
	c.Kind = graph.Kind(kind)
	return c, json.Unmarshal(attrs, &c.Attrs)
}

// Evidence is one reason an edge exists.
type Evidence struct {
	Source       graph.Source   `json:"source"`
	SourceRef    string         `json:"source_ref"`
	Confidence   float64        `json:"confidence"`
	Observations int64          `json:"observations"`
	Detail       map[string]any `json:"detail"`
	FirstSeenAt  time.Time      `json:"first_seen_at"`
	LastSeenAt   time.Time      `json:"last_seen_at"`
}

// Relation is an edge seen from one of its components.
type Relation struct {
	ID         string         `json:"id"`
	Type       graph.EdgeType `json:"type"`
	Direction  string         `json:"direction"` // "out": this component → other; "in": other → this
	Other      graph.Ref      `json:"component"`
	OtherID    string         `json:"component_id"`
	OtherLabel string         `json:"label"`
	Confidence float64        `json:"confidence"`
	Sources    []graph.Source `json:"sources"`
	Certain    bool           `json:"certain"`
	Evidence   []Evidence     `json:"evidence"`
}

// Relations returns a component's edges with their evidence, at most limit
// per direction (most recently seen first).
func (s *Store) Relations(ctx context.Context, c Component, limit int) ([]Relation, bool, error) {
	var out []Relation
	truncated := false
	for _, dir := range []string{"out", "in"} {
		self, other := "from_id", "to_id"
		if dir == "in" {
			self, other = "to_id", "from_id"
		}
		rows, err := s.Pool.Query(ctx, `
			SELECT e.id, e.type, e.confidence::float8, e.sources, o.id, o.kind, o.key, o.label
			FROM graph.dependency_edge e JOIN graph.component o ON o.id = e.`+other+`
			WHERE e.`+self+` = $1 ORDER BY e.last_seen_at DESC, e.id LIMIT $2`, c.ID, limit+1)
		if err != nil {
			return nil, false, err
		}
		rels, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Relation, error) {
			var r Relation
			var t, kind string
			var sources []string
			if err := row.Scan(&r.ID, &t, &r.Confidence, &sources, &r.OtherID, &kind, &r.Other.Key, &r.OtherLabel); err != nil {
				return r, err
			}
			r.Type, r.Other.Kind, r.Direction = graph.EdgeType(t), graph.Kind(kind), dir
			for _, s := range sources {
				r.Sources = append(r.Sources, graph.Source(s))
			}
			r.Certain = graph.Edge{Sources: r.Sources}.Certain()
			return r, nil
		})
		if err != nil {
			return nil, false, err
		}
		if len(rels) > limit {
			rels, truncated = rels[:limit], true
		}
		out = append(out, rels...)
	}
	if len(out) == 0 {
		return []Relation{}, truncated, nil
	}
	edgeIDs := make([]string, len(out))
	for i, r := range out {
		edgeIDs[i] = r.ID
	}
	rows, err := s.Pool.Query(ctx, `SELECT edge_id, source, source_ref, confidence::float8, observations, detail, first_seen_at, last_seen_at
		FROM graph.edge_evidence WHERE edge_id = ANY($1::uuid[]) ORDER BY source, source_ref`, edgeIDs)
	if err != nil {
		return nil, false, err
	}
	byEdge := map[string][]Evidence{}
	for rows.Next() {
		var edgeID, src string
		var ev Evidence
		var detail []byte
		if err := rows.Scan(&edgeID, &src, &ev.SourceRef, &ev.Confidence, &ev.Observations, &detail, &ev.FirstSeenAt, &ev.LastSeenAt); err != nil {
			return nil, false, err
		}
		ev.Source = graph.Source(src)
		if err := json.Unmarshal(detail, &ev.Detail); err != nil {
			return nil, false, err
		}
		byEdge[edgeID] = append(byEdge[edgeID], ev)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	for i := range out {
		out[i].Evidence = byEdge[out[i].ID]
		if out[i].Evidence == nil {
			out[i].Evidence = []Evidence{}
		}
		slices.SortFunc(out[i].Evidence, func(a, b Evidence) int {
			return strings.Compare(string(a.Source)+a.SourceRef, string(b.Source)+b.SourceRef)
		})
	}
	return out, truncated, nil
}
