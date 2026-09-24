package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

// Organization is a tenant.
type Organization struct {
	ID        string    `json:"id"`
	Slug      string    `json:"slug"`
	Name      string    `json:"name"`
	CreatedAt time.Time `json:"created_at"`
}

// User is a human account.
type User struct {
	ID          string     `json:"id"`
	Email       string     `json:"email"`
	DisplayName string     `json:"display_name"`
	OIDCSubject *string    `json:"-"`
	DisabledAt  *time.Time `json:"disabled_at,omitempty"`
}

// Membership links a user to an organization with a role.
type Membership struct {
	OrganizationID string     `json:"organization_id"`
	OrgSlug        string     `json:"organization_slug"`
	OrgName        string     `json:"organization_name"`
	Role           authn.Role `json:"role"`
}

// EnsureOrganization creates the organization if the slug is unused and returns it.
func (s *Store) EnsureOrganization(ctx context.Context, q db.Querier, slug, name string) (Organization, error) {
	var o Organization
	err := q.QueryRow(ctx, `
		INSERT INTO control.organization (id, slug, name) VALUES ($1, $2, $3)
		ON CONFLICT (slug) DO UPDATE SET name = control.organization.name
		RETURNING id, slug, name, created_at`, ids.New(), slug, name).Scan(&o.ID, &o.Slug, &o.Name, &o.CreatedAt)
	return o, mapErr(err)
}

// GetOrganization returns an organization by id.
func (s *Store) GetOrganization(ctx context.Context, id string) (Organization, error) {
	var o Organization
	err := s.Pool.QueryRow(ctx, `SELECT id, slug, name, created_at FROM control.organization WHERE id = $1`, id).
		Scan(&o.ID, &o.Slug, &o.Name, &o.CreatedAt)
	return o, mapErr(err)
}

// EnsureUser creates the user if the email is unused and returns it.
func (s *Store) EnsureUser(ctx context.Context, q db.Querier, email, displayName string) (User, error) {
	var u User
	err := q.QueryRow(ctx, `
		INSERT INTO control.app_user (id, email, display_name) VALUES ($1, lower($2), $3)
		ON CONFLICT (email) DO UPDATE SET display_name = control.app_user.display_name
		RETURNING id, email, display_name, oidc_subject, disabled_at`, ids.New(), email, displayName).
		Scan(&u.ID, &u.Email, &u.DisplayName, &u.OIDCSubject, &u.DisabledAt)
	return u, mapErr(err)
}

// EnsureMembership upserts the membership role.
func (s *Store) EnsureMembership(ctx context.Context, q db.Querier, orgID, userID string, role authn.Role) error {
	_, err := q.Exec(ctx, `
		INSERT INTO control.membership (organization_id, user_id, role) VALUES ($1, $2, $3)
		ON CONFLICT (organization_id, user_id) DO UPDATE SET role = EXCLUDED.role`, orgID, userID, string(role))
	return mapErr(err)
}

// GetUserByEmail looks a user up by email (case-insensitive).
func (s *Store) GetUserByEmail(ctx context.Context, email string) (User, error) {
	var u User
	err := s.Pool.QueryRow(ctx, `SELECT id, email, display_name, oidc_subject, disabled_at FROM control.app_user WHERE email = lower($1)`, email).
		Scan(&u.ID, &u.Email, &u.DisplayName, &u.OIDCSubject, &u.DisabledAt)
	return u, mapErr(err)
}

// GetUser returns a user by id.
func (s *Store) GetUser(ctx context.Context, id string) (User, error) {
	var u User
	err := s.Pool.QueryRow(ctx, `SELECT id, email, display_name, oidc_subject, disabled_at FROM control.app_user WHERE id = $1`, id).
		Scan(&u.ID, &u.Email, &u.DisplayName, &u.OIDCSubject, &u.DisabledAt)
	return u, mapErr(err)
}

// LinkOIDCSubject binds an OIDC subject to a user on first login (by verified email).
func (s *Store) LinkOIDCSubject(ctx context.Context, userID, subject string) error {
	_, err := s.Pool.Exec(ctx, `UPDATE control.app_user SET oidc_subject = $2 WHERE id = $1 AND (oidc_subject IS NULL OR oidc_subject = $2)`, userID, subject)
	return mapErr(err)
}

// GetUserByOIDCSubject finds a user by OIDC subject.
func (s *Store) GetUserByOIDCSubject(ctx context.Context, subject string) (User, error) {
	var u User
	err := s.Pool.QueryRow(ctx, `SELECT id, email, display_name, oidc_subject, disabled_at FROM control.app_user WHERE oidc_subject = $1`, subject).
		Scan(&u.ID, &u.Email, &u.DisplayName, &u.OIDCSubject, &u.DisabledAt)
	return u, mapErr(err)
}

// Memberships lists the organizations of a user.
func (s *Store) Memberships(ctx context.Context, userID string) ([]Membership, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT m.organization_id, o.slug, o.name, m.role
		FROM control.membership m JOIN control.organization o ON o.id = m.organization_id
		WHERE m.user_id = $1 ORDER BY o.slug`, userID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Membership, error) {
		var m Membership
		var role string
		err := r.Scan(&m.OrganizationID, &m.OrgSlug, &m.OrgName, &role)
		m.Role = authn.Role(role)
		return m, err
	})
}

// Membership returns the role of a user in an organization.
func (s *Store) Membership(ctx context.Context, orgID, userID string) (authn.Role, error) {
	var role string
	err := s.Pool.QueryRow(ctx, `SELECT role FROM control.membership WHERE organization_id = $1 AND user_id = $2`, orgID, userID).Scan(&role)
	return authn.Role(role), mapErr(err)
}

// OrgMember is a user with its role, for listings.
type OrgMember struct {
	User
	Role authn.Role `json:"role"`
}

// ListMembers lists the members of an organization.
func (s *Store) ListMembers(ctx context.Context, orgID string) ([]OrgMember, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT u.id, u.email, u.display_name, m.role FROM control.membership m
		JOIN control.app_user u ON u.id = m.user_id WHERE m.organization_id = $1 ORDER BY u.email`, orgID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (OrgMember, error) {
		var m OrgMember
		var role string
		err := r.Scan(&m.ID, &m.Email, &m.DisplayName, &role)
		m.Role = authn.Role(role)
		return m, err
	})
}
