package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
)

// Approval is an approval request as recorded.
type Approval struct {
	ID              string         `json:"id"`
	Organization    string         `json:"-"`
	Project         string         `json:"project_id"`
	Tool            string         `json:"tool"`
	Risk            string         `json:"risk"`
	Agent           string         `json:"agent"`
	AgentVersion    string         `json:"agent_version"`
	Environment     string         `json:"environment"`
	TraceID         *string        `json:"trace_id"`
	ActionHash      string         `json:"action_hash"`
	Arguments       map[string]any `json:"arguments"`
	Summary         string         `json:"summary"`
	PolicyName      string         `json:"policy"`
	PolicyVersionID *string        `json:"policy_version_id"`
	Rule            string         `json:"rule"`
	Reason          string         `json:"reason"`
	DecisionID      string         `json:"decision_id"`
	Status          string         `json:"status"`
	RequestedBy     string         `json:"requested_by"`
	ExpiresAt       time.Time      `json:"expires_at"`
	DecidedBy       *string        `json:"decided_by"`
	DecidedAt       *time.Time     `json:"decided_at"`
	DecisionReason  *string        `json:"decision_reason"`
	TokenHash       *string        `json:"-"`
	TokenExpiresAt  *time.Time     `json:"-"`
	UsedAt          *time.Time     `json:"used_at"`
	UsedDecisionID  *string        `json:"used_decision_id"`
	CreatedAt       time.Time      `json:"created_at"`
	UpdatedAt       time.Time      `json:"updated_at"`
}

const approvalCols = `id, organization_id, project_id, tool, risk, agent, agent_version, environment, trace_id,
	action_hash, arguments, summary, policy_name, policy_version_id, rule, reason, decision_id, status, requested_by,
	expires_at, decided_by, decided_at, decision_reason, token_hash, token_expires_at, used_at, used_decision_id,
	created_at, updated_at`

func scanApproval(row pgx.Row) (Approval, error) {
	var a Approval
	err := row.Scan(&a.ID, &a.Organization, &a.Project, &a.Tool, &a.Risk, &a.Agent, &a.AgentVersion, &a.Environment,
		&a.TraceID, &a.ActionHash, &a.Arguments, &a.Summary, &a.PolicyName, &a.PolicyVersionID, &a.Rule, &a.Reason,
		&a.DecisionID, &a.Status, &a.RequestedBy, &a.ExpiresAt, &a.DecidedBy, &a.DecidedAt, &a.DecisionReason,
		&a.TokenHash, &a.TokenExpiresAt, &a.UsedAt, &a.UsedDecisionID, &a.CreatedAt, &a.UpdatedAt)
	return a, err
}

// OpenApproval returns the pending request for the action, expiring one
// whose time has passed (it then returns ErrNotFound).
func (s *Store) OpenApproval(ctx context.Context, tx pgx.Tx, sc Scope, actionHash string, now time.Time) (Approval, error) {
	a, err := scanApproval(tx.QueryRow(ctx, `SELECT `+approvalCols+` FROM approval_request
		WHERE organization_id = $1 AND project_id = $2 AND action_hash = $3 AND status = 'PENDING' FOR UPDATE`,
		sc.OrgID, sc.ProjectID, actionHash))
	if errors.Is(err, pgx.ErrNoRows) {
		return Approval{}, ErrNotFound
	}
	if err != nil {
		return Approval{}, err
	}
	if !now.Before(a.ExpiresAt) {
		if err := s.SetStatus(ctx, tx, a.ID, "EXPIRED", now); err != nil {
			return Approval{}, err
		}
		return Approval{}, ErrNotFound
	}
	return a, nil
}

// InsertApproval records a new approval request.
func (s *Store) InsertApproval(ctx context.Context, tx pgx.Tx, sc Scope, a Approval) error {
	_, err := tx.Exec(ctx, `INSERT INTO approval_request (id, organization_id, project_id, tool, risk, agent,
			agent_version, environment, trace_id, action_hash, arguments, summary, policy_name, policy_version_id, rule,
			reason, decision_id, status, requested_by, expires_at, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, 'PENDING', $18, $19, $20, $20)`,
		a.ID, sc.OrgID, sc.ProjectID, a.Tool, a.Risk, a.Agent, a.AgentVersion, a.Environment, a.TraceID, a.ActionHash,
		mustJSON(a.Arguments), a.Summary, a.PolicyName, a.PolicyVersionID, a.Rule, a.Reason, a.DecisionID,
		a.RequestedBy, a.ExpiresAt, a.CreatedAt)
	if isUnique(err) {
		return ErrConflict
	}
	return err
}

// Approval returns a request, or ErrNotFound. forUpdate locks it.
func (s *Store) Approval(ctx context.Context, q Querier, sc Scope, id string, forUpdate bool) (Approval, error) {
	sql := `SELECT ` + approvalCols + ` FROM approval_request WHERE organization_id = $1 AND project_id = $2 AND id = $3`
	if forUpdate {
		sql += ` FOR UPDATE`
	}
	a, err := scanApproval(q.QueryRow(ctx, sql, sc.OrgID, sc.ProjectID, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Approval{}, ErrNotFound
	}
	return a, err
}

// ApprovalByToken returns and locks the request a token was minted for.
// The organization check is the caller's (a token names no scope itself).
func (s *Store) ApprovalByToken(ctx context.Context, tx pgx.Tx, tokenHash string) (Approval, error) {
	a, err := scanApproval(tx.QueryRow(ctx, `SELECT `+approvalCols+` FROM approval_request WHERE token_hash = $1 FOR UPDATE`, tokenHash))
	if errors.Is(err, pgx.ErrNoRows) {
		return Approval{}, ErrNotFound
	}
	return a, err
}

// SetStatus moves a request to status (the schema refuses any move but forward).
func (s *Store) SetStatus(ctx context.Context, q Querier, id, status string, at time.Time) error {
	_, err := q.Exec(ctx, `UPDATE approval_request SET status = $2, updated_at = $3 WHERE id = $1`, id, status, at)
	return err
}

// Decide records a person's decision on a pending request, or returns
// ErrConflict when it is no longer pending.
func (s *Store) Decide(ctx context.Context, tx pgx.Tx, id, status, by, reason string, at time.Time) error {
	tag, err := tx.Exec(ctx, `UPDATE approval_request SET status = $2, decided_by = $3, decision_reason = $4,
		decided_at = $5, updated_at = $5 WHERE id = $1 AND status = 'PENDING'`, id, status, by, nullable(reason), at)
	if err == nil && tag.RowsAffected() != 1 {
		return ErrConflict
	}
	return err
}

// SetToken stores the hash of a newly minted token (voiding the previous
// one), or returns ErrConflict when the request is not approved.
func (s *Store) SetToken(ctx context.Context, tx pgx.Tx, id, hash string, expires, at time.Time) error {
	tag, err := tx.Exec(ctx, `UPDATE approval_request SET token_hash = $2, token_expires_at = $3, updated_at = $4
		WHERE id = $1 AND status = 'APPROVED'`, id, hash, expires, at)
	if err == nil && tag.RowsAffected() != 1 {
		return ErrConflict
	}
	return err
}

// Use marks an approved request used by the decision that executed it.
func (s *Store) Use(ctx context.Context, tx pgx.Tx, id, decisionID string, at time.Time) error {
	tag, err := tx.Exec(ctx, `UPDATE approval_request SET status = 'USED', used_at = $3, used_decision_id = $2,
		updated_at = $3 WHERE id = $1 AND status = 'APPROVED'`, id, decisionID, at)
	if err == nil && tag.RowsAffected() != 1 {
		return ErrConflict
	}
	return err
}

// ApprovalFilter narrows a list of requests. Status EXPIRED also matches
// open requests past their expiry, and PENDING/APPROVED exclude them.
type ApprovalFilter struct {
	Status string
	Tool   string
}

// CountPending is the number of approval requests, in every project, that
// wait for a person and have not reached their expiry (a gauge of the
// gateway's metrics, not an API answer).
func (s *Store) CountPending(ctx context.Context, now time.Time) (int, error) {
	var n int
	err := s.Pool.QueryRow(ctx, `SELECT count(*) FROM approval_request WHERE status = 'PENDING' AND expires_at > $1`, now).Scan(&n)
	return n, err
}

// Approvals lists requests, newest first, from after.
func (s *Store) Approvals(ctx context.Context, sc Scope, f ApprovalFilter, now time.Time, after *httpx.Cursor, limit int) ([]Approval, error) {
	var afterTS any
	afterID := ""
	if after != nil {
		afterTS, afterID = after.TS, after.ID
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+approvalCols+` FROM approval_request
		WHERE organization_id = $1 AND project_id = $2 AND ($3 = '' OR tool = $3)
		  AND CASE $4
		        WHEN '' THEN true
		        WHEN 'EXPIRED' THEN status = 'EXPIRED' OR (status IN ('PENDING', 'APPROVED') AND expires_at <= $5)
		        WHEN 'PENDING' THEN status = 'PENDING' AND expires_at > $5
		        WHEN 'APPROVED' THEN status = 'APPROVED' AND expires_at > $5
		        ELSE status = $4 END
		  AND ($6::timestamptz IS NULL OR (created_at, id) < ($6, $7::uuid))
		ORDER BY created_at DESC, id DESC LIMIT $8`,
		sc.OrgID, sc.ProjectID, f.Tool, f.Status, now, afterTS, nullable(afterID), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Approval{}
	for rows.Next() {
		a, err := scanApproval(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// Attempt is a call that presented an approval's token.
type Attempt struct {
	ID         string         `json:"id"`
	ApprovalID string         `json:"-"`
	Result     string         `json:"result"`
	ActionHash string         `json:"action_hash"`
	Arguments  map[string]any `json:"arguments"`
	Subject    string         `json:"subject"`
	TraceID    *string        `json:"trace_id"`
	DecisionID *string        `json:"decision_id"`
	CreatedAt  time.Time      `json:"created_at"`
}

// InsertAttempt records an attempt.
func (s *Store) InsertAttempt(ctx context.Context, q Querier, sc Scope, a Attempt) error {
	_, err := q.Exec(ctx, `INSERT INTO approval_attempt (id, approval_id, organization_id, project_id, result,
			action_hash, arguments, subject, trace_id, decision_id, created_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
		a.ID, a.ApprovalID, sc.OrgID, sc.ProjectID, a.Result, a.ActionHash, mustJSON(a.Arguments), a.Subject,
		a.TraceID, a.DecisionID, a.CreatedAt)
	return err
}

// Attempts lists the attempts made with a request's token, oldest first.
func (s *Store) Attempts(ctx context.Context, approvalID string) ([]Attempt, error) {
	rows, err := s.Pool.Query(ctx, `SELECT id, result, action_hash, arguments, subject, trace_id, decision_id, created_at
		FROM approval_attempt WHERE approval_id = $1 ORDER BY created_at, id`, approvalID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Attempt{}
	for rows.Next() {
		var a Attempt
		if err := rows.Scan(&a.ID, &a.Result, &a.ActionHash, &a.Arguments, &a.Subject, &a.TraceID, &a.DecisionID, &a.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------- idempotency

// Idem is an idempotency record.
type Idem struct {
	ActionHash          string
	State               string
	ResponseStatus      *int
	ResponseBody        []byte
	ResponseContentType *string
	DecisionID          string
	UpdatedAt           time.Time
}

// IdemRetention is how long an idempotency key is remembered.
const IdemRetention = 24 * time.Hour

// LockIdem returns and locks the key's live record (nil when there is none
// or it has expired).
func (s *Store) LockIdem(ctx context.Context, tx pgx.Tx, sc Scope, tool, key string, now time.Time) (*Idem, error) {
	var r Idem
	err := tx.QueryRow(ctx, `SELECT action_hash, state, response_status, response_body, response_content_type,
			decision_id, updated_at
		FROM idempotency_record WHERE organization_id = $1 AND project_id = $2 AND tool = $3 AND key = $4
		  AND expires_at > $5 FOR UPDATE`, sc.OrgID, sc.ProjectID, tool, key, now).
		Scan(&r.ActionHash, &r.State, &r.ResponseStatus, &r.ResponseBody, &r.ResponseContentType, &r.DecisionID, &r.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// ClaimIdem marks the key in progress for the action (a new or expired key,
// or one whose previous outcome is unknown).
func (s *Store) ClaimIdem(ctx context.Context, tx pgx.Tx, sc Scope, tool, key, actionHash, decisionID string, now time.Time) error {
	_, err := tx.Exec(ctx, `INSERT INTO idempotency_record (organization_id, project_id, tool, key, action_hash, state,
			decision_id, created_at, updated_at, expires_at)
		VALUES ($1, $2, $3, $4, $5, 'IN_PROGRESS', $6, $7, $7, $8)
		ON CONFLICT (organization_id, project_id, tool, key) DO UPDATE SET action_hash = EXCLUDED.action_hash,
			state = 'IN_PROGRESS', response_status = NULL, response_body = NULL, response_content_type = NULL,
			decision_id = EXCLUDED.decision_id, updated_at = EXCLUDED.updated_at,
			created_at = CASE WHEN idempotency_record.expires_at <= EXCLUDED.updated_at THEN EXCLUDED.created_at
				ELSE idempotency_record.created_at END,
			expires_at = CASE WHEN idempotency_record.expires_at <= EXCLUDED.updated_at THEN EXCLUDED.expires_at
				ELSE idempotency_record.expires_at END`,
		sc.OrgID, sc.ProjectID, tool, key, actionHash, decisionID, now, now.Add(IdemRetention))
	return err
}

// FinishIdem records how the key's call ended (the response is kept when it
// completed, for replays).
func (s *Store) FinishIdem(ctx context.Context, q Querier, sc Scope, tool, key, decisionID, state string, status *int,
	body []byte, contentType string, now time.Time) error {
	if state != "COMPLETED" {
		body, contentType = nil, ""
	}
	_, err := q.Exec(ctx, `UPDATE idempotency_record SET state = $6, response_status = $7, response_body = $8,
			response_content_type = $9, updated_at = $10
		WHERE organization_id = $1 AND project_id = $2 AND tool = $3 AND key = $4 AND decision_id = $5`,
		sc.OrgID, sc.ProjectID, tool, key, decisionID, state, status, body, nullable(contentType), now)
	return err
}
