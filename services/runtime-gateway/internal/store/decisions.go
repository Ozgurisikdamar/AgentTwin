package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
)

// Decision outcomes.
const (
	OutcomeForwarded        = "forwarded"
	OutcomeExecuted         = "executed"
	OutcomeFailed           = "failed"
	OutcomeReplayed         = "replayed"
	OutcomeDenied           = "denied"
	OutcomeApprovalRequired = "approval_required"
	OutcomeApprovalRefused  = "approval_refused"
)

// Decision is one decision of the gateway, as recorded.
type Decision struct {
	ID              string          `json:"id"`
	Tool            string          `json:"tool"`
	Risk            string          `json:"risk"`
	Agent           string          `json:"agent"`
	AgentVersion    string          `json:"agent_version"`
	Environment     string          `json:"environment"`
	Subject         string          `json:"subject"`
	TraceID         *string         `json:"trace_id"`
	ActionHash      string          `json:"action_hash"`
	Arguments       map[string]any  `json:"arguments"`
	Summary         string          `json:"summary"`
	Effect          *string         `json:"effect"`
	Outcome         string          `json:"outcome"`
	PolicyName      *string         `json:"policy"`
	PolicyVersionID *string         `json:"policy_version_id"`
	Rule            *string         `json:"rule"`
	Message         string          `json:"message"`
	Decisions       json.RawMessage `json:"decisions"`
	FailModeApplied bool            `json:"fail_mode_applied"`
	ApprovalID      *string         `json:"approval_id"`
	IdempotencyKey  *string         `json:"idempotency_key"`
	Limits          json.RawMessage `json:"limits"`
	UpstreamStatus  *int            `json:"upstream_status"`
	ErrorCode       *string         `json:"error_code"`
	LatencyMs       *int            `json:"latency_ms"`
	CreatedAt       time.Time       `json:"created_at"`
	CompletedAt     *time.Time      `json:"completed_at"`
}

const decisionCols = `id, tool, risk, agent, agent_version, environment, subject, trace_id, action_hash, arguments,
	summary, effect, outcome, policy_name, policy_version_id, rule, message, decisions, fail_mode_applied, approval_id,
	idempotency_key, limits, upstream_status, error_code, latency_ms, created_at, completed_at`

func scanDecision(row pgx.Row) (Decision, error) {
	var d Decision
	var limits []byte
	err := row.Scan(&d.ID, &d.Tool, &d.Risk, &d.Agent, &d.AgentVersion, &d.Environment, &d.Subject, &d.TraceID,
		&d.ActionHash, &d.Arguments, &d.Summary, &d.Effect, &d.Outcome, &d.PolicyName, &d.PolicyVersionID, &d.Rule,
		&d.Message, &d.Decisions, &d.FailModeApplied, &d.ApprovalID, &d.IdempotencyKey, &limits, &d.UpstreamStatus,
		&d.ErrorCode, &d.LatencyMs, &d.CreatedAt, &d.CompletedAt)
	if limits != nil {
		d.Limits = limits
	} else {
		d.Limits = json.RawMessage("null")
	}
	return d, err
}

// InsertDecision records a decision.
func (s *Store) InsertDecision(ctx context.Context, q Querier, sc Scope, d Decision) error {
	decisions := d.Decisions
	if decisions == nil {
		decisions = json.RawMessage("[]")
	}
	var limits any
	if len(d.Limits) > 0 && string(d.Limits) != "null" {
		limits = []byte(d.Limits)
	}
	_, err := q.Exec(ctx, `INSERT INTO policy_decision (id, organization_id, project_id, tool, risk, agent, agent_version,
			environment, subject, trace_id, action_hash, arguments, summary, effect, outcome, policy_name,
			policy_version_id, rule, message, decisions, fail_mode_applied, approval_id, idempotency_key, limits,
			upstream_status, error_code, latency_ms, created_at, completed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22,
			$23, $24, $25, $26, $27, $28, $29)`,
		d.ID, sc.OrgID, sc.ProjectID, d.Tool, d.Risk, d.Agent, d.AgentVersion, d.Environment, d.Subject, d.TraceID,
		d.ActionHash, mustJSON(d.Arguments), d.Summary, d.Effect, d.Outcome, d.PolicyName, d.PolicyVersionID, d.Rule,
		d.Message, []byte(decisions), d.FailModeApplied, d.ApprovalID, d.IdempotencyKey, limits, d.UpstreamStatus,
		d.ErrorCode, d.LatencyMs, d.CreatedAt, d.CompletedAt)
	return err
}

// CompleteDecision records how a forwarded call ended.
func (s *Store) CompleteDecision(ctx context.Context, q Querier, id, outcome string, status *int, errorCode string,
	latencyMs int, at time.Time) error {
	tag, err := q.Exec(ctx, `UPDATE policy_decision SET outcome = $2, upstream_status = $3, error_code = $4,
		latency_ms = $5, completed_at = $6 WHERE id = $1 AND outcome = 'forwarded'`,
		id, outcome, status, nullable(errorCode), latencyMs, at)
	if err == nil && tag.RowsAffected() != 1 {
		return ErrNotFound
	}
	return err
}

// DecisionFilter narrows a list of decisions.
type DecisionFilter struct {
	Tool       string
	Outcome    string
	Effect     string
	TraceID    string
	ApprovalID string
}

// Decisions lists decisions, newest first, from after.
func (s *Store) Decisions(ctx context.Context, sc Scope, f DecisionFilter, after *httpx.Cursor, limit int) ([]Decision, error) {
	var afterTS any
	afterID := ""
	if after != nil {
		afterTS, afterID = after.TS, after.ID
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+decisionCols+` FROM policy_decision
		WHERE organization_id = $1 AND project_id = $2
		  AND ($3 = '' OR tool = $3) AND ($4 = '' OR outcome = $4) AND ($5 = '' OR effect = $5)
		  AND ($6 = '' OR trace_id = $6) AND ($7 = '' OR approval_id::text = $7)
		  AND ($8::timestamptz IS NULL OR (created_at, id) < ($8, $9::uuid))
		ORDER BY created_at DESC, id DESC LIMIT $10`,
		sc.OrgID, sc.ProjectID, f.Tool, f.Outcome, f.Effect, f.TraceID, f.ApprovalID, afterTS, nullable(afterID), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Decision{}
	for rows.Next() {
		d, err := scanDecision(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// Decision returns one decision, or ErrNotFound.
func (s *Store) Decision(ctx context.Context, sc Scope, id string) (Decision, error) {
	d, err := scanDecision(s.Pool.QueryRow(ctx, `SELECT `+decisionCols+` FROM policy_decision
		WHERE organization_id = $1 AND project_id = $2 AND id = $3`, sc.OrgID, sc.ProjectID, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Decision{}, ErrNotFound
	}
	return d, err
}

// ---------------------------------------------------------------- per-trace calls

// TraceCalls counts the other actions of the tool forwarded in the trace.
func (s *Store) TraceCalls(ctx context.Context, q Querier, sc Scope, traceID, tool, actionHash string) (int, error) {
	if traceID == "" {
		return 0, nil
	}
	var n int
	err := q.QueryRow(ctx, `SELECT count(*) FROM trace_tool_call WHERE organization_id = $1 AND project_id = $2
		AND trace_id = $3 AND tool = $4 AND action_hash <> $5`, sc.OrgID, sc.ProjectID, traceID, tool, actionHash).Scan(&n)
	return n, err
}

// RecordTraceCall notes that the action was forwarded in the trace.
func (s *Store) RecordTraceCall(ctx context.Context, q Querier, sc Scope, traceID, tool, actionHash string, at time.Time) error {
	if traceID == "" {
		return nil
	}
	_, err := q.Exec(ctx, `INSERT INTO trace_tool_call (organization_id, project_id, trace_id, tool, action_hash, first_at)
		VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING`, sc.OrgID, sc.ProjectID, traceID, tool, actionHash, at)
	return err
}
