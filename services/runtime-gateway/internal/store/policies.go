package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

// Policy is a named policy that guards one tool.
type Policy struct {
	ID            string     `json:"id"`
	Name          string     `json:"name"`
	Tool          string     `json:"tool"`
	Description   string     `json:"description"`
	LatestVersion int        `json:"latest_version"`
	Active        *VersionOf `json:"active"`
	ActivatedBy   *string    `json:"activated_by"`
	ActivatedAt   *time.Time `json:"activated_at"`
	CreatedBy     string     `json:"created_by"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
}

// VersionOf names one version of a policy.
type VersionOf struct {
	ID      string `json:"id"`
	Version int    `json:"version"`
}

// Version is an immutable version of a policy.
type Version struct {
	ID        string          `json:"id"`
	PolicyID  string          `json:"policy_id"`
	Version   int             `json:"version"`
	Document  string          `json:"document"`
	Spec      json.RawMessage `json:"spec"`
	SpecHash  string          `json:"spec_hash"`
	CreatedBy string          `json:"created_by"`
	CreatedAt time.Time       `json:"created_at"`
}

const policyCols = `p.id, p.name, p.tool, p.description, p.latest_version, p.active_version_id,
	(SELECT v.version FROM policy_version v WHERE v.id = p.active_version_id), p.activated_by, p.activated_at,
	p.created_by, p.created_at, p.updated_at`

func scanPolicy(row pgx.Row) (Policy, error) {
	var p Policy
	var activeID *string
	var activeVersion *int
	err := row.Scan(&p.ID, &p.Name, &p.Tool, &p.Description, &p.LatestVersion, &activeID, &activeVersion,
		&p.ActivatedBy, &p.ActivatedAt, &p.CreatedBy, &p.CreatedAt, &p.UpdatedAt)
	if activeID != nil && activeVersion != nil {
		p.Active = &VersionOf{ID: *activeID, Version: *activeVersion}
	}
	return p, err
}

const versionCols = `id, policy_id, version, document, spec, spec_hash, created_by, created_at`

func scanVersion(row pgx.Row) (Version, error) {
	var v Version
	err := row.Scan(&v.ID, &v.PolicyID, &v.Version, &v.Document, &v.Spec, &v.SpecHash, &v.CreatedBy, &v.CreatedAt)
	return v, err
}

// CreatePolicy stores a policy with its first version, or returns
// ErrConflict when the name is taken in the project.
func (s *Store) CreatePolicy(ctx context.Context, tx pgx.Tx, sc Scope, p Policy, v Version) error {
	_, err := tx.Exec(ctx, `INSERT INTO policy (id, organization_id, project_id, name, tool, description,
			latest_version, created_by, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, 1, $7, $8, $8)`,
		p.ID, sc.OrgID, sc.ProjectID, p.Name, p.Tool, p.Description, p.CreatedBy, p.CreatedAt)
	if isUnique(err) {
		return ErrConflict
	}
	if err != nil {
		return err
	}
	return insertVersion(ctx, tx, v)
}

func insertVersion(ctx context.Context, tx pgx.Tx, v Version) error {
	_, err := tx.Exec(ctx, `INSERT INTO policy_version (`+versionCols+`) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		v.ID, v.PolicyID, v.Version, v.Document, []byte(v.Spec), v.SpecHash, v.CreatedBy, v.CreatedAt)
	return err
}

// Policy returns the policy, or ErrNotFound. forUpdate locks it.
func (s *Store) Policy(ctx context.Context, q Querier, sc Scope, id string, forUpdate bool) (Policy, error) {
	sql := `SELECT ` + policyCols + ` FROM policy p WHERE p.organization_id = $1 AND p.project_id = $2 AND p.id = $3`
	if forUpdate {
		sql += ` FOR UPDATE OF p`
	}
	p, err := scanPolicy(q.QueryRow(ctx, sql, sc.OrgID, sc.ProjectID, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Policy{}, ErrNotFound
	}
	return p, err
}

// Policies lists the project's policies by name, optionally for one tool.
func (s *Store) Policies(ctx context.Context, sc Scope, tool string) ([]Policy, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+policyCols+` FROM policy p
		WHERE p.organization_id = $1 AND p.project_id = $2 AND ($3 = '' OR p.tool = $3) ORDER BY p.name`,
		sc.OrgID, sc.ProjectID, tool)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Policy{}
	for rows.Next() {
		p, err := scanPolicy(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// Versions lists a policy's versions, newest first.
func (s *Store) Versions(ctx context.Context, policyID string) ([]Version, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+versionCols+` FROM policy_version WHERE policy_id = $1 ORDER BY version DESC`, policyID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Version{}
	for rows.Next() {
		v, err := scanVersion(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// Version returns one version of a policy, or ErrNotFound.
func (s *Store) Version(ctx context.Context, q Querier, policyID string, version int) (Version, error) {
	v, err := scanVersion(q.QueryRow(ctx, `SELECT `+versionCols+` FROM policy_version WHERE policy_id = $1 AND version = $2`,
		policyID, version))
	if errors.Is(err, pgx.ErrNoRows) {
		return Version{}, ErrNotFound
	}
	return v, err
}

// AddVersion stores the next version of a locked policy.
func (s *Store) AddVersion(ctx context.Context, tx pgx.Tx, p Policy, v Version) error {
	if err := insertVersion(ctx, tx, v); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `UPDATE policy SET latest_version = $2, description = $3, updated_at = $4 WHERE id = $1`,
		p.ID, v.Version, p.Description, v.CreatedAt)
	return err
}

// Activate makes a version the policy's active one (nil deactivates it).
func (s *Store) Activate(ctx context.Context, tx pgx.Tx, policyID string, versionID *string, by string, at time.Time) error {
	var who any = by
	var when any = at
	if versionID == nil {
		who, when = nil, nil
	}
	_, err := tx.Exec(ctx, `UPDATE policy SET active_version_id = $2, activated_by = $3, activated_at = $4, updated_at = $5
		WHERE id = $1`, policyID, versionID, who, when, at)
	return err
}

// ActiveVersion is an active policy version that guards a tool.
type ActiveVersion struct {
	PolicyName string
	VersionID  string
	Version    int
	Spec       json.RawMessage
}

// ActiveVersions lists the active versions guarding a tool, by policy name.
func (s *Store) ActiveVersions(ctx context.Context, q Querier, sc Scope, tool string) ([]ActiveVersion, error) {
	rows, err := q.Query(ctx, `SELECT p.name, v.id, v.version, v.spec FROM policy p
		JOIN policy_version v ON v.id = p.active_version_id
		WHERE p.organization_id = $1 AND p.project_id = $2 AND p.tool = $3 ORDER BY p.name`,
		sc.OrgID, sc.ProjectID, tool)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ActiveVersion{}
	for rows.Next() {
		var a ActiveVersion
		if err := rows.Scan(&a.PolicyName, &a.VersionID, &a.Version, &a.Spec); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
