package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
)

// APIKey is a stored (hashed) project API key. The secret is never stored.
type APIKey struct {
	ID             string        `json:"id"`
	OrganizationID string        `json:"organization_id"`
	ProjectID      string        `json:"project_id"`
	Name           string        `json:"name"`
	Prefix         string        `json:"prefix"`
	SecretHash     string        `json:"-"`
	Scopes         []authn.Scope `json:"scopes"`
	CreatedBy      string        `json:"created_by"`
	CreatedAt      time.Time     `json:"created_at"`
	ExpiresAt      *time.Time    `json:"expires_at,omitempty"`
	RevokedAt      *time.Time    `json:"revoked_at,omitempty"`
	RevokedBy      *string       `json:"revoked_by,omitempty"`
	LastUsedAt     *time.Time    `json:"last_used_at,omitempty"`
	RotatedFrom    *string       `json:"rotated_from,omitempty"`
}

const apiKeyCols = `id, organization_id, project_id, name, prefix, secret_hash, scopes, created_by, created_at,
	expires_at, revoked_at, revoked_by, last_used_at, rotated_from`

func scanAPIKey(r pgx.Row) (APIKey, error) {
	var k APIKey
	var scopes []string
	err := r.Scan(&k.ID, &k.OrganizationID, &k.ProjectID, &k.Name, &k.Prefix, &k.SecretHash, &scopes, &k.CreatedBy,
		&k.CreatedAt, &k.ExpiresAt, &k.RevokedAt, &k.RevokedBy, &k.LastUsedAt, &k.RotatedFrom)
	for _, s := range scopes {
		k.Scopes = append(k.Scopes, authn.Scope(s))
	}
	return k, mapErr(err)
}

// InsertAPIKey stores a new key.
func (s *Store) InsertAPIKey(ctx context.Context, q db.Querier, k APIKey) (APIKey, error) {
	scopes := make([]string, 0, len(k.Scopes))
	for _, sc := range k.Scopes {
		scopes = append(scopes, string(sc))
	}
	return scanAPIKey(q.QueryRow(ctx, `
		INSERT INTO control.api_key (id, organization_id, project_id, name, prefix, secret_hash, scopes, created_by, expires_at, rotated_from)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
		RETURNING `+apiKeyCols, k.ID, k.OrganizationID, k.ProjectID, k.Name, k.Prefix, k.SecretHash, scopes, k.CreatedBy, k.ExpiresAt, k.RotatedFrom))
}

// UpsertDemoAPIKey (development seeding) creates or refreshes a key with a fixed prefix.
func (s *Store) UpsertDemoAPIKey(ctx context.Context, q db.Querier, k APIKey) error {
	scopes := make([]string, 0, len(k.Scopes))
	for _, sc := range k.Scopes {
		scopes = append(scopes, string(sc))
	}
	_, err := q.Exec(ctx, `
		INSERT INTO control.api_key (id, organization_id, project_id, name, prefix, secret_hash, scopes, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		ON CONFLICT (prefix) DO UPDATE SET secret_hash = EXCLUDED.secret_hash, scopes = EXCLUDED.scopes, revoked_at = NULL, revoked_by = NULL`,
		k.ID, k.OrganizationID, k.ProjectID, k.Name, k.Prefix, k.SecretHash, scopes, k.CreatedBy)
	return mapErr(err)
}

// GetAPIKeyByPrefix looks a key up by its public prefix (unscoped: the caller
// has not authenticated yet; the hash comparison authenticates).
func (s *Store) GetAPIKeyByPrefix(ctx context.Context, prefix string) (APIKey, error) {
	return scanAPIKey(s.Pool.QueryRow(ctx, `SELECT `+apiKeyCols+` FROM control.api_key WHERE prefix = $1`, prefix))
}

// GetAPIKey returns a key of the organization.
func (s *Store) GetAPIKey(ctx context.Context, orgID, id string) (APIKey, error) {
	return scanAPIKey(s.Pool.QueryRow(ctx, `SELECT `+apiKeyCols+` FROM control.api_key WHERE organization_id = $1 AND id = $2`, orgID, id))
}

// ListAPIKeys lists keys of a project (never returns secrets).
func (s *Store) ListAPIKeys(ctx context.Context, orgID, projectID string) ([]APIKey, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+apiKeyCols+` FROM control.api_key WHERE organization_id = $1 AND project_id = $2 ORDER BY created_at DESC`, orgID, projectID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (APIKey, error) { return scanAPIKey(r) })
}

// RevokeAPIKey revokes a key; revoking twice is a no-op.
func (s *Store) RevokeAPIKey(ctx context.Context, q db.Querier, orgID, id, actor string) (APIKey, error) {
	return scanAPIKey(q.QueryRow(ctx, `
		UPDATE control.api_key SET revoked_at = COALESCE(revoked_at, now()), revoked_by = COALESCE(revoked_by, $3)
		WHERE organization_id = $1 AND id = $2 RETURNING `+apiKeyCols, orgID, id, actor))
}

// TouchAPIKey records usage, at most once per minute per key to avoid write amplification.
func (s *Store) TouchAPIKey(ctx context.Context, id string) error {
	_, err := s.Pool.Exec(ctx, `UPDATE control.api_key SET last_used_at = now() WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')`, id)
	return err
}
