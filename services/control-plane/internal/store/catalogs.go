package store

import (
	"context"
	"encoding/json"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
)

// CatalogSummary is a tool catalog without its entries (lists).
type CatalogSummary struct {
	ID             string          `json:"id"`
	ProjectID      string          `json:"project_id"`
	Source         string          `json:"source"`
	Name           string          `json:"name"`
	Revision       int             `json:"revision"`
	Service        *string         `json:"service"`
	Title          string          `json:"title"`
	APIVersion     string          `json:"api_version"`
	SpecVersion    string          `json:"spec_version"`
	Summary        json.RawMessage `json:"summary"`
	ContentSHA256  string          `json:"content_sha256"`
	DocumentSHA256 string          `json:"document_sha256"`
	CreatedBy      string          `json:"created_by"`
	CreatedAt      time.Time       `json:"created_at"`
}

// Catalog is a stored tool catalog.
type Catalog struct {
	CatalogSummary
	Servers  json.RawMessage `json:"servers"`
	Options  json.RawMessage `json:"options"`
	Entries  json.RawMessage `json:"entries"`
	Skipped  json.RawMessage `json:"skipped"`
	Warnings json.RawMessage `json:"warnings"`
}

// NewCatalog is what InsertCatalog stores.
type NewCatalog struct {
	ID, OrganizationID, ProjectID, Source, Name string
	Revision                                    int
	Service                                     *string
	Title, APIVersion, SpecVersion              string
	Servers, Options, Entries, Skipped          any
	Warnings, Summary                           any
	ContentSHA256, DocumentSHA256               string
	CreatedBy                                   string
}

const catalogSummaryCols = `id, project_id, source, name, revision, service, title, api_version, spec_version, summary,
	content_sha256, document_sha256, created_by, created_at`

func catalogSummaryDest(c *CatalogSummary) []any {
	return []any{&c.ID, &c.ProjectID, &c.Source, &c.Name, &c.Revision, &c.Service, &c.Title, &c.APIVersion, &c.SpecVersion,
		&c.Summary, &c.ContentSHA256, &c.DocumentSHA256, &c.CreatedBy, &c.CreatedAt}
}

// LockCatalogSource serializes imports of one source of a project until
// the transaction ends, so revisions are numbered without gaps or races.
func (s *Store) LockCatalogSource(ctx context.Context, tx pgx.Tx, orgID, projectID, source, name string) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended('tool_catalog:' || $1 || ':' || $2 || ':' || $3 || ':' || $4, 0))`,
		orgID, projectID, source, name)
	return err
}

// LatestCatalog returns the latest revision of a project's source.
func (s *Store) LatestCatalog(ctx context.Context, q db.Querier, orgID, projectID, source, name string) (Catalog, error) {
	var c Catalog
	dest := append(catalogSummaryDest(&c.CatalogSummary), &c.Servers, &c.Options, &c.Entries, &c.Skipped, &c.Warnings)
	err := q.QueryRow(ctx, `SELECT `+catalogSummaryCols+`, servers, options, entries, skipped, warnings FROM control.tool_catalog
		WHERE organization_id = $1 AND project_id = $2 AND source = $3 AND name = $4 ORDER BY revision DESC LIMIT 1`,
		orgID, projectID, source, name).Scan(dest...)
	return c, mapErr(err)
}

// InsertCatalog stores a catalog revision.
func (s *Store) InsertCatalog(ctx context.Context, q db.Querier, c NewCatalog) error {
	enc := func(v any) []byte {
		b, _ := json.Marshal(v)
		return b
	}
	_, err := q.Exec(ctx, `
		INSERT INTO control.tool_catalog (id, organization_id, project_id, source, name, revision, service, title, api_version,
			spec_version, servers, options, entries, skipped, warnings, summary, content_sha256, document_sha256, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)`,
		c.ID, c.OrganizationID, c.ProjectID, c.Source, c.Name, c.Revision, c.Service, c.Title, c.APIVersion, c.SpecVersion,
		enc(c.Servers), enc(c.Options), enc(c.Entries), enc(c.Skipped), enc(c.Warnings), enc(c.Summary),
		c.ContentSHA256, c.DocumentSHA256, c.CreatedBy)
	return mapErr(err)
}

// GetCatalog returns a catalog of the organization.
func (s *Store) GetCatalog(ctx context.Context, q db.Querier, orgID, id string) (Catalog, error) {
	var c Catalog
	dest := append(catalogSummaryDest(&c.CatalogSummary), &c.Servers, &c.Options, &c.Entries, &c.Skipped, &c.Warnings)
	err := q.QueryRow(ctx, `SELECT `+catalogSummaryCols+`, servers, options, entries, skipped, warnings FROM control.tool_catalog
		WHERE organization_id = $1 AND id = $2`, orgID, id).Scan(dest...)
	return c, mapErr(err)
}

// CatalogFilter selects a page of catalogs, newest first.
type CatalogFilter struct {
	ProjectID string
	Source    string // optional
	Name      string // optional
	Before    *time.Time
	BeforeID  string
	Limit     int
}

// ListCatalogs returns a page of a project's catalogs, newest first.
func (s *Store) ListCatalogs(ctx context.Context, orgID string, f CatalogFilter) ([]CatalogSummary, error) {
	args := []any{orgID, f.ProjectID, f.Limit}
	where := `WHERE organization_id = $1 AND project_id = $2`
	add := func(cond string, v ...any) {
		args = append(args, v...)
		where += " AND " + cond
	}
	if f.Source != "" {
		add(`source = $`+strconv.Itoa(len(args)+1), f.Source)
	}
	if f.Name != "" {
		add(`name = $`+strconv.Itoa(len(args)+1), f.Name)
	}
	if f.Before != nil {
		n := len(args)
		add(`(created_at, id) < ($`+strconv.Itoa(n+1)+`, $`+strconv.Itoa(n+2)+`)`, *f.Before, f.BeforeID)
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+catalogSummaryCols+` FROM control.tool_catalog `+where+
		` ORDER BY created_at DESC, id DESC LIMIT $3`, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (CatalogSummary, error) {
		var c CatalogSummary
		err := r.Scan(catalogSummaryDest(&c)...)
		return c, err
	})
}

// ToolOwnership says who defines a registry tool: whether an agent
// manifest declares it, and where its latest version came from.
type ToolOwnership struct {
	Exists       bool
	Manifest     bool
	LatestSource string
	LatestRef    string
}

// GetToolOwnership returns the ownership of a project's tool by name.
func (s *Store) GetToolOwnership(ctx context.Context, q db.Querier, orgID, projectID, name string) (ToolOwnership, error) {
	var o ToolOwnership
	var source, ref *string
	err := q.QueryRow(ctx, `
		SELECT EXISTS (SELECT 1 FROM control.tool_version WHERE tool_id = t.id AND source = 'MANIFEST'),
			v.source, v.source_ref
		FROM control.tool t
		LEFT JOIN LATERAL (SELECT source, source_ref FROM control.tool_version WHERE tool_id = t.id ORDER BY version DESC LIMIT 1) v ON true
		WHERE t.organization_id = $1 AND t.project_id = $2 AND t.name = $3`, orgID, projectID, name).
		Scan(&o.Manifest, &source, &ref)
	if db.IsNoRows(err) {
		return ToolOwnership{}, nil
	}
	if err != nil {
		return ToolOwnership{}, err
	}
	o.Exists = true
	o.LatestSource, o.LatestRef = deref(source), deref(ref)
	return o, nil
}
