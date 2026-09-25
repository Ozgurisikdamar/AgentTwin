package store

import (
	"context"
	"encoding/json"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
)

// ChangeSetVersion names one side of a change set.
type ChangeSetVersion struct {
	ID      string `json:"id"`
	Version string `json:"version"`
}

// ChangeSetSummary is a change set without its items (lists).
type ChangeSetSummary struct {
	ID            string           `json:"id"`
	ProjectID     string           `json:"project_id"`
	AgentID       string           `json:"agent_id"`
	AgentName     string           `json:"agent_name"`
	Base          ChangeSetVersion `json:"base"`
	Candidate     ChangeSetVersion `json:"candidate"`
	Title         string           `json:"title"`
	Summary       json.RawMessage  `json:"summary"`
	ContentSHA256 string           `json:"content_sha256"`
	CreatedBy     string           `json:"created_by"`
	CreatedAt     time.Time        `json:"created_at"`
}

// ChangeSet is a stored change set.
type ChangeSet struct {
	ChangeSetSummary
	Git      json.RawMessage `json:"git"`
	Declared json.RawMessage `json:"declared"`
	Items    json.RawMessage `json:"items"`
	Seeds    json.RawMessage `json:"seeds"`
	Scope    json.RawMessage `json:"scope"`
}

// NewChangeSet is what InsertChangeSet stores.
type NewChangeSet struct {
	ID, OrganizationID, ProjectID, AgentID string
	BaseVersionID, CandidateVersionID      string
	Title                                  string
	Git                                    any // nil: none
	Declared, Items, Seeds, Scope, Summary any
	ContentSHA256                          string
	CreatedBy                              string
}

const changeSetSummaryCols = `c.id, c.project_id, c.agent_id, a.name, c.base_version_id, bv.version,
	c.candidate_version_id, cv.version, c.title, c.summary, c.content_sha256, c.created_by, c.created_at`

const changeSetFrom = ` FROM control.change_set c
	JOIN control.agent a ON a.id = c.agent_id
	JOIN control.agent_version bv ON bv.id = c.base_version_id
	JOIN control.agent_version cv ON cv.id = c.candidate_version_id `

func summaryDest(s *ChangeSetSummary) []any {
	return []any{&s.ID, &s.ProjectID, &s.AgentID, &s.AgentName, &s.Base.ID, &s.Base.Version,
		&s.Candidate.ID, &s.Candidate.Version, &s.Title, &s.Summary, &s.ContentSHA256, &s.CreatedBy, &s.CreatedAt}
}

// InsertChangeSet stores a change set unless the agent already has one with
// the same content; inserted reports which.
func (s *Store) InsertChangeSet(ctx context.Context, q db.Querier, c NewChangeSet) (inserted bool, err error) {
	enc := func(v any) []byte {
		b, _ := json.Marshal(v)
		return b
	}
	var git []byte
	if c.Git != nil {
		git = enc(c.Git)
	}
	tag, err := q.Exec(ctx, `
		INSERT INTO control.change_set (id, organization_id, project_id, agent_id, base_version_id, candidate_version_id,
			title, git, declared, items, seeds, scope, summary, content_sha256, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
		ON CONFLICT (agent_id, content_sha256) DO NOTHING`,
		c.ID, c.OrganizationID, c.ProjectID, c.AgentID, c.BaseVersionID, c.CandidateVersionID, c.Title, git,
		enc(c.Declared), enc(c.Items), enc(c.Seeds), enc(c.Scope), enc(c.Summary), c.ContentSHA256, c.CreatedBy)
	if err != nil {
		return false, mapErr(err)
	}
	return tag.RowsAffected() == 1, nil
}

// GetChangeSet returns a change set of the organization.
func (s *Store) GetChangeSet(ctx context.Context, q db.Querier, orgID, id string) (ChangeSet, error) {
	var c ChangeSet
	dest := append(summaryDest(&c.ChangeSetSummary), &c.Git, &c.Declared, &c.Items, &c.Seeds, &c.Scope)
	err := q.QueryRow(ctx, `SELECT `+changeSetSummaryCols+`, c.git, c.declared, c.items, c.seeds, c.scope`+changeSetFrom+
		`WHERE c.organization_id = $1 AND c.id = $2`, orgID, id).Scan(dest...)
	return c, mapErr(err)
}

// ChangeSetIDByContent finds the change set of an agent with this content.
func (s *Store) ChangeSetIDByContent(ctx context.Context, q db.Querier, orgID, agentID, sha string) (string, error) {
	var id string
	err := q.QueryRow(ctx, `SELECT id FROM control.change_set WHERE organization_id = $1 AND agent_id = $2 AND content_sha256 = $3`,
		orgID, agentID, sha).Scan(&id)
	return id, mapErr(err)
}

// ChangeSetFilter selects a page of change sets, newest first.
type ChangeSetFilter struct {
	ProjectID string
	AgentID   string // optional
	Before    *time.Time
	BeforeID  string
	Limit     int
}

// ListChangeSets returns a page of a project's change sets, newest first.
func (s *Store) ListChangeSets(ctx context.Context, orgID string, f ChangeSetFilter) ([]ChangeSetSummary, error) {
	args := []any{orgID, f.ProjectID, f.Limit}
	where := `WHERE c.organization_id = $1 AND c.project_id = $2`
	if f.AgentID != "" {
		args = append(args, f.AgentID)
		where += ` AND c.agent_id = $4`
	}
	if f.Before != nil {
		args = append(args, *f.Before, f.BeforeID)
		n := len(args)
		where += ` AND (c.created_at, c.id) < ($` + strconv.Itoa(n-1) + `, $` + strconv.Itoa(n) + `)`
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+changeSetSummaryCols+changeSetFrom+where+
		` ORDER BY c.created_at DESC, c.id DESC LIMIT $3`, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (ChangeSetSummary, error) {
		var c ChangeSetSummary
		err := r.Scan(summaryDest(&c)...)
		return c, err
	})
}
