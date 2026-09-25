package store

import (
	"context"
	"encoding/json"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
)

// ReleaseAgent names a release's agent.
type ReleaseAgent struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Release is a candidate version of an agent against its baseline.
type Release struct {
	ID          string           `json:"id"`
	ProjectID   string           `json:"project_id"`
	Agent       ReleaseAgent     `json:"agent"`
	ChangeSetID string           `json:"change_set_id"`
	Baseline    ChangeSetVersion `json:"baseline"`
	Candidate   ChangeSetVersion `json:"candidate"`
	Title       string           `json:"title"`
	CommitSHA   *string          `json:"commit_sha"`
	CIURL       *string          `json:"ci_url"`
	CreatedBy   string           `json:"created_by"`
	CreatedAt   time.Time        `json:"created_at"`
	// Changes are the counts of the release's change set (what lists show
	// as its changed components).
	Changes json.RawMessage `json:"changes"`
}

// Revision summarizes one evaluation of a release, with its decision and
// override when there are.
type Revision struct {
	ID          string     `json:"id"`
	ReleaseID   string     `json:"release_id"`
	Revision    int        `json:"revision"`
	Status      string     `json:"status"`
	RequestedBy string     `json:"requested_by"`
	RequestedAt time.Time  `json:"requested_at"`
	EvalRunID   *string    `json:"eval_run_id"`
	DecidedAt   *time.Time `json:"decided_at"`
	// Decision and Override are set once decided (and overridden).
	Decision *DecisionSummary `json:"-"`
	Override *Override        `json:"-"`
}

// DecisionSummary is a gate decision without the decision document.
type DecisionSummary struct {
	ID             string          `json:"id"`
	Outcome        string          `json:"outcome"`
	Incomplete     bool            `json:"incomplete"`
	RulesVersion   string          `json:"rules_version"`
	RiskIndex      int             `json:"risk_index"`
	Summary        json.RawMessage `json:"summary"`
	EvidenceSHA256 string          `json:"evidence_sha256"`
	DecidedAt      time.Time       `json:"decided_at"`
}

// Evaluation is one revision with what it rests on.
type Evaluation struct {
	Revision
	ProjectID string          `json:"project_id"`
	Policy    json.RawMessage `json:"policy"`
	Impact    json.RawMessage `json:"impact"`
	Suite     json.RawMessage `json:"suite"`
	Tools     json.RawMessage `json:"tools"`
}

// Decision is a stored gate decision.
type Decision struct {
	DecisionSummary
	Decision json.RawMessage `json:"decision"`
	Input    json.RawMessage `json:"input"`
}

// Override records who let a gated release through, and why.
type Override struct {
	ID              string     `json:"id"`
	GateDecisionID  string     `json:"gate_decision_id"`
	OriginalOutcome string     `json:"original_outcome"`
	Reason          string     `json:"reason"`
	TicketURL       *string    `json:"ticket_url"`
	ExpiresAt       *time.Time `json:"expires_at"`
	Actor           string     `json:"actor"`
	CreatedAt       time.Time  `json:"created_at"`
}

// NewRelease is what InsertRelease stores.
type NewRelease struct {
	ID, OrganizationID, ProjectID, AgentID, ChangeSetID string
	BaselineVersionID, CandidateVersionID               string
	Title                                               string
	CommitSHA, CIURL                                    *string
	CreatedBy                                           string
}

// NewEvaluation is what InsertEvaluation stores.
type NewEvaluation struct {
	ID, OrganizationID, ProjectID, ReleaseID string
	Revision                                 int
	RequestedBy                              string
	Policy, Impact, Suite, Tools             any
}

// NewDecision is what InsertDecision stores.
type NewDecision struct {
	ID, OrganizationID, ProjectID, ReleaseID, EvaluationID string
	Outcome                                                string
	Incomplete                                             bool
	RulesVersion                                           string
	RiskIndex                                              int
	Decision, Input, Summary                               any
	EvidenceSHA256                                         string
}

// NewOverride is what InsertOverride stores.
type NewOverride struct {
	ID, OrganizationID, ProjectID, ReleaseID, DecisionID string
	OriginalOutcome, Reason                              string
	TicketURL                                            *string
	ExpiresAt                                            *time.Time
	Actor                                                string
}

func enc(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}

const releaseCols = `r.id, r.project_id, r.agent_id, a.name, r.change_set_id, r.baseline_version_id, bv.version,
	r.candidate_version_id, cv.version, r.title, r.commit_sha, r.ci_url, r.created_by, r.created_at, c.summary`

const releaseFrom = ` FROM control.release r
	JOIN control.agent a ON a.id = r.agent_id
	JOIN control.agent_version bv ON bv.id = r.baseline_version_id
	JOIN control.agent_version cv ON cv.id = r.candidate_version_id
	JOIN control.change_set c ON c.id = r.change_set_id `

func releaseDest(r *Release) []any {
	return []any{&r.ID, &r.ProjectID, &r.Agent.ID, &r.Agent.Name, &r.ChangeSetID, &r.Baseline.ID, &r.Baseline.Version,
		&r.Candidate.ID, &r.Candidate.Version, &r.Title, &r.CommitSHA, &r.CIURL, &r.CreatedBy, &r.CreatedAt, &r.Changes}
}

// InsertRelease stores a release.
func (s *Store) InsertRelease(ctx context.Context, q db.Querier, r NewRelease) error {
	_, err := q.Exec(ctx, `
		INSERT INTO control.release (id, organization_id, project_id, agent_id, change_set_id, baseline_version_id,
			candidate_version_id, title, commit_sha, ci_url, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
		r.ID, r.OrganizationID, r.ProjectID, r.AgentID, r.ChangeSetID, r.BaselineVersionID, r.CandidateVersionID,
		r.Title, r.CommitSHA, r.CIURL, r.CreatedBy)
	return mapErr(err)
}

// GetRelease returns a release of the organization.
func (s *Store) GetRelease(ctx context.Context, q db.Querier, orgID, id string) (Release, error) {
	var r Release
	err := q.QueryRow(ctx, `SELECT `+releaseCols+releaseFrom+`WHERE r.organization_id = $1 AND r.id = $2`, orgID, id).
		Scan(releaseDest(&r)...)
	return r, mapErr(err)
}

// LockRelease serializes the evaluations of a release (row lock only: a
// release is never updated).
func (s *Store) LockRelease(ctx context.Context, tx pgx.Tx, orgID, id string) error {
	var got string
	err := tx.QueryRow(ctx, `SELECT id FROM control.release WHERE organization_id = $1 AND id = $2 FOR UPDATE`,
		orgID, id).Scan(&got)
	return mapErr(err)
}

// ReleaseFilter selects a page of releases, newest first.
type ReleaseFilter struct {
	ProjectID string
	AgentID   string // optional
	Before    *time.Time
	BeforeID  string
	Limit     int
}

// ListReleases returns a page of a project's releases, newest first.
func (s *Store) ListReleases(ctx context.Context, orgID string, f ReleaseFilter) ([]Release, error) {
	args := []any{orgID, f.ProjectID, f.Limit}
	where := `WHERE r.organization_id = $1 AND r.project_id = $2`
	if f.AgentID != "" {
		args = append(args, f.AgentID)
		where += ` AND r.agent_id = $` + strconv.Itoa(len(args))
	}
	if f.Before != nil {
		args = append(args, *f.Before, f.BeforeID)
		n := len(args)
		where += ` AND (r.created_at, r.id) < ($` + strconv.Itoa(n-1) + `, $` + strconv.Itoa(n) + `)`
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+releaseCols+releaseFrom+where+
		` ORDER BY r.created_at DESC, r.id DESC LIMIT $3`, args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Release, error) {
		var r Release
		err := row.Scan(releaseDest(&r)...)
		return r, err
	})
}

const revisionCols = `e.id, e.release_id, e.revision, e.status, e.requested_by, e.requested_at, e.eval_run_id, e.decided_at,
	d.id, d.outcome, d.incomplete, d.rules_version, d.risk_index, d.summary, d.evidence_sha256, d.decided_at,
	o.id, o.gate_decision_id, o.original_outcome, o.reason, o.ticket_url, o.expires_at, o.actor, o.created_at`

const revisionFrom = ` FROM control.release_evaluation e
	LEFT JOIN control.gate_decision d ON d.release_evaluation_id = e.id
	LEFT JOIN control.gate_override o ON o.gate_decision_id = d.id `

// revisionRow scans the revision columns; the decision and override are
// set only when present.
type revisionRow struct {
	rev Revision
	d   struct {
		id, outcome, rules, sha *string
		incomplete              *bool
		risk                    *int
		summary                 json.RawMessage
		at                      *time.Time
	}
	o struct {
		id, decision, original, reason, actor *string
		ticket                                *string
		expires, at                           *time.Time
	}
}

func (x *revisionRow) dest() []any {
	r := &x.rev
	return []any{&r.ID, &r.ReleaseID, &r.Revision, &r.Status, &r.RequestedBy, &r.RequestedAt, &r.EvalRunID, &r.DecidedAt,
		&x.d.id, &x.d.outcome, &x.d.incomplete, &x.d.rules, &x.d.risk, &x.d.summary, &x.d.sha, &x.d.at,
		&x.o.id, &x.o.decision, &x.o.original, &x.o.reason, &x.o.ticket, &x.o.expires, &x.o.actor, &x.o.at}
}

func (x *revisionRow) revision() Revision {
	r := x.rev
	if x.d.id != nil {
		r.Decision = &DecisionSummary{ID: *x.d.id, Outcome: *x.d.outcome, Incomplete: *x.d.incomplete,
			RulesVersion: *x.d.rules, RiskIndex: *x.d.risk, Summary: x.d.summary, EvidenceSHA256: *x.d.sha, DecidedAt: *x.d.at}
	}
	if x.o.id != nil {
		r.Override = &Override{ID: *x.o.id, GateDecisionID: *x.o.decision, OriginalOutcome: *x.o.original,
			Reason: *x.o.reason, TicketURL: x.o.ticket, ExpiresAt: x.o.expires, Actor: *x.o.actor, CreatedAt: *x.o.at}
	}
	return r
}

// Revisions lists a release's evaluations, newest first.
func (s *Store) Revisions(ctx context.Context, q db.Querier, orgID, releaseID string) ([]Revision, error) {
	rows, err := q.Query(ctx, `SELECT `+revisionCols+revisionFrom+
		`WHERE e.organization_id = $1 AND e.release_id = $2 ORDER BY e.revision DESC`, orgID, releaseID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Revision, error) {
		var x revisionRow
		err := row.Scan(x.dest()...)
		return x.revision(), err
	})
}

// LatestRevisions returns the latest evaluation of each release.
func (s *Store) LatestRevisions(ctx context.Context, orgID string, releaseIDs []string) (map[string]Revision, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+revisionCols+revisionFrom+
		`WHERE e.organization_id = $1 AND e.release_id = ANY($2)
		   AND e.revision = (SELECT max(revision) FROM control.release_evaluation l WHERE l.release_id = e.release_id)`,
		orgID, releaseIDs)
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Revision, error) {
		var x revisionRow
		err := row.Scan(x.dest()...)
		return x.revision(), err
	})
	out := make(map[string]Revision, len(list))
	for _, r := range list {
		out[r.ReleaseID] = r
	}
	return out, err
}

// NextRevision is the number of a release's next evaluation (call with the
// release locked).
func (s *Store) NextRevision(ctx context.Context, tx pgx.Tx, releaseID string) (int, error) {
	var n int
	err := tx.QueryRow(ctx, `SELECT COALESCE(max(revision), 0) + 1 FROM control.release_evaluation WHERE release_id = $1`,
		releaseID).Scan(&n)
	return n, err
}

// InsertEvaluation stores a requested evaluation (EVALUATING).
func (s *Store) InsertEvaluation(ctx context.Context, q db.Querier, e NewEvaluation) error {
	_, err := q.Exec(ctx, `
		INSERT INTO control.release_evaluation (id, organization_id, project_id, release_id, revision, status,
			requested_by, policy, impact, suite, tools)
		VALUES ($1, $2, $3, $4, $5, 'EVALUATING', $6, $7, $8, $9, $10)`,
		e.ID, e.OrganizationID, e.ProjectID, e.ReleaseID, e.Revision, e.RequestedBy,
		enc(e.Policy), enc(e.Impact), enc(e.Suite), enc(e.Tools))
	return mapErr(err)
}

const evaluationCols = `e.project_id, e.policy, e.impact, e.suite, e.tools, ` + revisionCols

func scanEvaluation(r pgx.Row) (Evaluation, error) {
	var e Evaluation
	var x revisionRow
	err := r.Scan(append([]any{&e.ProjectID, &e.Policy, &e.Impact, &e.Suite, &e.Tools}, x.dest()...)...)
	e.Revision = x.revision()
	return e, mapErr(err)
}

// GetEvaluation returns an evaluation of the organization.
func (s *Store) GetEvaluation(ctx context.Context, q db.Querier, orgID, id string) (Evaluation, error) {
	return scanEvaluation(q.QueryRow(ctx, `SELECT `+evaluationCols+revisionFrom+
		`WHERE e.organization_id = $1 AND e.id = $2`, orgID, id))
}

// EvaluationOf returns a release's evaluation by revision (0: the latest).
func (s *Store) EvaluationOf(ctx context.Context, q db.Querier, orgID, releaseID string, revision int) (Evaluation, error) {
	if revision > 0 {
		return scanEvaluation(q.QueryRow(ctx, `SELECT `+evaluationCols+revisionFrom+
			`WHERE e.organization_id = $1 AND e.release_id = $2 AND e.revision = $3`, orgID, releaseID, revision))
	}
	return scanEvaluation(q.QueryRow(ctx, `SELECT `+evaluationCols+revisionFrom+
		`WHERE e.organization_id = $1 AND e.release_id = $2 ORDER BY e.revision DESC LIMIT 1`, orgID, releaseID))
}

// MarkDecided moves an evaluation from EVALUATING to DECIDED, recording its
// run; false when it was decided before.
func (s *Store) MarkDecided(ctx context.Context, tx pgx.Tx, orgID, id string, evalRunID *string) (bool, error) {
	tag, err := tx.Exec(ctx, `UPDATE control.release_evaluation SET status = 'DECIDED', decided_at = now(),
			eval_run_id = COALESCE($3, eval_run_id)
		WHERE organization_id = $1 AND id = $2 AND status = 'EVALUATING'`, orgID, id, evalRunID)
	if err != nil {
		return false, mapErr(err)
	}
	return tag.RowsAffected() == 1, nil
}

// InsertDecision stores an evaluation's gate decision.
func (s *Store) InsertDecision(ctx context.Context, tx pgx.Tx, d NewDecision) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO control.gate_decision (id, organization_id, project_id, release_id, release_evaluation_id, outcome,
			incomplete, rules_version, risk_index, decision, input, summary, evidence_sha256)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`,
		d.ID, d.OrganizationID, d.ProjectID, d.ReleaseID, d.EvaluationID, d.Outcome, d.Incomplete, d.RulesVersion,
		d.RiskIndex, enc(d.Decision), enc(d.Input), enc(d.Summary), d.EvidenceSHA256)
	return mapErr(err)
}

// GetDecision returns a gate decision with its document and input.
func (s *Store) GetDecision(ctx context.Context, q db.Querier, orgID, id string) (Decision, error) {
	var d Decision
	err := q.QueryRow(ctx, `SELECT id, outcome, incomplete, rules_version, risk_index, summary, evidence_sha256,
			decided_at, decision, input
		FROM control.gate_decision WHERE organization_id = $1 AND id = $2`, orgID, id).
		Scan(&d.ID, &d.Outcome, &d.Incomplete, &d.RulesVersion, &d.RiskIndex, &d.Summary, &d.EvidenceSHA256,
			&d.DecidedAt, &d.Decision, &d.Input)
	return d, mapErr(err)
}

// InsertOverride stores an override; ErrConflict when the decision was
// overridden before.
func (s *Store) InsertOverride(ctx context.Context, tx pgx.Tx, o NewOverride) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO control.gate_override (id, organization_id, project_id, release_id, gate_decision_id,
			original_outcome, reason, ticket_url, expires_at, actor)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
		o.ID, o.OrganizationID, o.ProjectID, o.ReleaseID, o.DecisionID, o.OriginalOutcome, o.Reason, o.TicketURL,
		o.ExpiresAt, o.Actor)
	return mapErr(err)
}
