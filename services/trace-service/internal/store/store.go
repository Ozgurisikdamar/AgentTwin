// Package store persists traces in the trace schema.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/summary"
)

// ErrNotFound is returned for missing rows.
var ErrNotFound = errors.New("not found")

// ErrAmbiguous is returned when a trace id exists in several projects.
var ErrAmbiguous = errors.New("trace id exists in several projects")

// Store wraps the pool.
type Store struct{ Pool *pgxpool.Pool }

// New creates a store.
func New(pool *pgxpool.Pool) *Store { return &Store{Pool: pool} }

// Target is the tenant context of an ingest request (from the API key).
type Target struct {
	OrganizationID   string
	ProjectID        string
	ContentMode      string
	TraceRetention   time.Duration
	ContentRetention time.Duration
}

type traceSeed struct {
	id                        string
	start, end                time.Time
	root                      *model.Span
	sdk, sdkVersion           string
	simRun, scenario, session string
	contentDropped, truncated bool
	// Span-level values win over resource-level ones anywhere in the batch.
	span, res seedContext
}

// seedContext is trace-scoped metadata from one attribute level.
type seedContext struct {
	env, agent, version, release, commit, source string
}

func (c *seedContext) merge(env, agent, version, release, commit, source string) {
	c.env = firstNonEmpty(c.env, env)
	c.agent = firstNonEmpty(c.agent, agent)
	c.version = firstNonEmpty(c.version, version)
	c.release = firstNonEmpty(c.release, release)
	c.commit = firstNonEmpty(c.commit, commit)
	if validSources[source] {
		c.source = firstNonEmpty(c.source, source)
	}
}

func (sd *traceSeed) pick(f func(seedContext) string) string {
	return firstNonEmpty(f(sd.span), f(sd.res))
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

var validSources = map[string]bool{"production": true, "simulation": true, "replay": true, "eval": true, "test": true}

// Ingest stores spans in one transaction. Re-delivered spans are ignored
// (at-least-once OTLP retries are safe). It returns the number of new spans.
func (s *Store) Ingest(ctx context.Context, tgt Target, spans []model.Span, now time.Time) (int, error) {
	if len(spans) == 0 {
		return 0, nil
	}
	seeds := map[string]*traceSeed{}
	order := []string{}
	for i := range spans {
		sp := &spans[i]
		sd := seeds[sp.TraceID]
		if sd == nil {
			sd = &traceSeed{id: sp.TraceID, start: sp.Start, end: sp.End}
			seeds[sp.TraceID] = sd
			order = append(order, sp.TraceID)
		}
		if sp.Start.Before(sd.start) {
			sd.start = sp.Start
		}
		if sp.End.After(sd.end) {
			sd.end = sp.End
		}
		r := sp.Resource
		a := &sp.Attrs
		sd.span.merge(a.Environment, a.AgentName, a.AgentVersion, a.ReleaseID, a.CommitSHA, a.Source)
		sd.res.merge(r.Environment, r.AgentName, r.AgentVersion, r.ReleaseID, r.CommitSHA, r.Source)
		sd.sdk = firstNonEmpty(sd.sdk, r.SDKName)
		sd.sdkVersion = firstNonEmpty(sd.sdkVersion, r.SDKVersion)
		sd.simRun = firstNonEmpty(sd.simRun, sp.Attrs.SimulationRunID)
		sd.scenario = firstNonEmpty(sd.scenario, sp.Attrs.ScenarioID)
		sd.session = firstNonEmpty(sd.session, sp.Attrs.SessionID)
		sd.contentDropped = sd.contentDropped || sp.ContentDropped
		sd.truncated = sd.truncated || sp.Truncated
		if sp.IsRoot() {
			sd.root = sp
		}
	}
	sort.Strings(order) // stable lock order across concurrent requests (no deadlocks)
	inserted := 0
	err := db.WithTx(ctx, s.Pool, func(tx pgx.Tx) error {
		for _, id := range order {
			sd := seeds[id]
			var rootID, rootName *string
			if sd.root != nil {
				rootID, rootName = &sd.root.SpanID, &sd.root.Name
			}
			source := firstNonEmpty(sd.pick(func(c seedContext) string { return c.source }), "production")
			if _, err := tx.Exec(ctx, `
				INSERT INTO trace.trace (project_id, trace_id, organization_id, environment, agent_name, agent_version, session_id,
					release_id, commit_sha, source, simulation_run_id, scenario_id, root_span_id, root_name, root_received,
					started_at, ended_at, sdk_name, sdk_version, content_mode, content_dropped, truncated,
					expires_at, content_expires_at)
				VALUES ($1, $2, $3, COALESCE(NULLIF($4, ''), 'unknown'), NULLIF($5, ''), NULLIF($6, ''), NULLIF($7, ''),
					NULLIF($8, ''), NULLIF($9, ''), $10, NULLIF($11, ''), NULLIF($12, ''), $13::text, $14::text, $13::text IS NOT NULL,
					$15, $16, NULLIF($17, ''), NULLIF($18, ''), $19, $20, $21, $22, $23)
				ON CONFLICT (project_id, trace_id) DO UPDATE SET
					environment   = CASE WHEN trace.environment = 'unknown' THEN EXCLUDED.environment ELSE trace.environment END,
					agent_name    = COALESCE(trace.agent_name, EXCLUDED.agent_name),
					agent_version = COALESCE(trace.agent_version, EXCLUDED.agent_version),
					session_id    = COALESCE(trace.session_id, EXCLUDED.session_id),
					release_id    = COALESCE(trace.release_id, EXCLUDED.release_id),
					commit_sha    = COALESCE(trace.commit_sha, EXCLUDED.commit_sha),
					simulation_run_id = COALESCE(trace.simulation_run_id, EXCLUDED.simulation_run_id),
					scenario_id   = COALESCE(trace.scenario_id, EXCLUDED.scenario_id),
					root_span_id  = COALESCE(trace.root_span_id, EXCLUDED.root_span_id),
					root_name     = COALESCE(trace.root_name, EXCLUDED.root_name),
					root_received = trace.root_received OR EXCLUDED.root_received,
					started_at    = LEAST(trace.started_at, EXCLUDED.started_at),
					ended_at      = GREATEST(trace.ended_at, EXCLUDED.ended_at),
					sdk_name      = COALESCE(trace.sdk_name, EXCLUDED.sdk_name),
					sdk_version   = COALESCE(trace.sdk_version, EXCLUDED.sdk_version),
					content_dropped = trace.content_dropped OR EXCLUDED.content_dropped,
					truncated     = trace.truncated OR EXCLUDED.truncated,
					revision      = trace.revision + 1,
					dirty         = true,
					finalize_attempts = 0,
					updated_at    = now()`,
				tgt.ProjectID, id, tgt.OrganizationID, sd.pick(func(c seedContext) string { return c.env }),
				sd.pick(func(c seedContext) string { return c.agent }), sd.pick(func(c seedContext) string { return c.version }), sd.session,
				sd.pick(func(c seedContext) string { return c.release }), sd.pick(func(c seedContext) string { return c.commit }), source, sd.simRun, sd.scenario, rootID, rootName,
				sd.start, sd.end, sd.sdk, sd.sdkVersion, tgt.ContentMode, sd.contentDropped, sd.truncated,
				now.Add(tgt.TraceRetention), now.Add(tgt.ContentRetention)); err != nil {
				return fmt.Errorf("upsert trace: %w", err)
			}
		}
		n, err := insertSpans(ctx, tx, tgt.ProjectID, spans)
		if err != nil {
			return err
		}
		inserted = n
		_, err = tx.Exec(ctx, `
			UPDATE trace.trace t SET span_count = c.n
			FROM (SELECT trace_id, count(*) AS n FROM trace.span WHERE project_id = $1 AND trace_id = ANY($2) GROUP BY trace_id) c
			WHERE t.project_id = $1 AND t.trace_id = c.trace_id`, tgt.ProjectID, order)
		return err
	})
	return inserted, err
}

func insertSpans(ctx context.Context, tx pgx.Tx, projectID string, spans []model.Span) (int, error) {
	n := len(spans)
	traceIDs, spanIDs, names, kinds, statuses, semver := make([]string, n), make([]string, n), make([]string, n), make([]string, n), make([]string, n), make([]string, n)
	parents, statusMsgs, toolNames, toolRisks, models, decisions := make([]*string, n), make([]*string, n), make([]*string, n), make([]*string, n), make([]*string, n), make([]*string, n)
	otelKinds := make([]int16, n)
	starts, ends := make([]time.Time, n), make([]time.Time, n)
	durations := make([]float64, n)
	attrs, evs := make([]string, n), make([]string, n)
	contents := make([]*string, n)
	opt := func(s string) *string {
		if s == "" {
			return nil
		}
		return &s
	}
	for i, sp := range spans {
		traceIDs[i], spanIDs[i], names[i], kinds[i], statuses[i], semver[i] = sp.TraceID, sp.SpanID, truncate(sp.Name, 512), string(sp.Kind), string(sp.Status), sp.SemconvVersion
		parents[i], statusMsgs[i] = opt(sp.ParentSpanID), opt(sp.StatusMessage)
		toolNames[i], toolRisks[i], models[i], decisions[i] = opt(sp.Attrs.ToolName), opt(sp.Attrs.ToolRisk), opt(sp.Model()), opt(sp.Attrs.PolicyDecision)
		otelKinds[i] = int16(min(max(sp.OTelKind, 0), 5)) //nolint:gosec // bounded to the OTel span kinds 0..5
		starts[i], ends[i], durations[i] = sp.Start, sp.End, sp.DurationMS()
		a, err := json.Marshal(sp.Attrs)
		if err != nil {
			return 0, err
		}
		attrs[i] = string(a)
		ev := []model.Event{}
		if sp.Events != nil {
			ev = sp.Events
		}
		e, err := json.Marshal(ev)
		if err != nil {
			return 0, err
		}
		evs[i] = string(e)
		if !sp.Content.Empty() {
			c, err := json.Marshal(sp.Content)
			if err != nil {
				return 0, err
			}
			cs := string(c)
			contents[i] = &cs
		}
	}
	tag, err := tx.Exec(ctx, `
		INSERT INTO trace.span (project_id, trace_id, span_id, parent_span_id, name, kind, otel_kind, status, status_message,
			started_at, ended_at, duration_ms, tool_name, tool_risk, model, policy_decision, attributes, events, content, semconv_version)
		SELECT $1, t.trace_id, t.span_id, t.parent_span_id, t.name, t.kind, t.otel_kind, t.status, t.status_message,
			t.started_at, t.ended_at, t.duration_ms, t.tool_name, t.tool_risk, t.model, t.policy_decision,
			t.attributes::jsonb, t.events::jsonb, t.content::jsonb, t.semconv_version
		FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[], $7::smallint[], $8::text[], $9::text[],
			$10::timestamptz[], $11::timestamptz[], $12::float8[], $13::text[], $14::text[], $15::text[], $16::text[],
			$17::text[], $18::text[], $19::text[], $20::text[])
			AS t(trace_id, span_id, parent_span_id, name, kind, otel_kind, status, status_message, started_at, ended_at,
				duration_ms, tool_name, tool_risk, model, policy_decision, attributes, events, content, semconv_version)
		ON CONFLICT (project_id, trace_id, span_id) DO NOTHING`,
		projectID, traceIDs, spanIDs, parents, names, kinds, otelKinds, statuses, statusMsgs, starts, ends, durations,
		toolNames, toolRisks, models, decisions, attrs, evs, contents, semver)
	if err != nil {
		return 0, fmt.Errorf("insert spans: %w", err)
	}
	return int(tag.RowsAffected()), nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n]
}

// Trace is a trace row as returned by the API.
type Trace struct {
	ProjectID       string          `json:"project_id"`
	TraceID         string          `json:"trace_id"`
	OrganizationID  string          `json:"organization_id"`
	Environment     string          `json:"environment"`
	AgentName       *string         `json:"agent_name"`
	AgentVersion    *string         `json:"agent_version"`
	SessionID       *string         `json:"session_id"`
	ReleaseID       *string         `json:"release_id"`
	CommitSHA       *string         `json:"commit_sha"`
	Source          string          `json:"source"`
	SimulationRunID *string         `json:"simulation_run_id"`
	ScenarioID      *string         `json:"scenario_id"`
	RootSpanID      *string         `json:"root_span_id"`
	RootName        *string         `json:"root_name"`
	Status          string          `json:"status"`
	StartedAt       time.Time       `json:"started_at"`
	EndedAt         *time.Time      `json:"ended_at"`
	DurationMS      *float64        `json:"duration_ms"`
	SpanCount       int             `json:"span_count"`
	ModelCallCount  int             `json:"model_call_count"`
	ToolCallCount   int             `json:"tool_call_count"`
	ErrorCount      int             `json:"error_count"`
	RetryCount      int             `json:"retry_count"`
	InputTokens     int64           `json:"input_tokens"`
	OutputTokens    int64           `json:"output_tokens"`
	CostUSD         *float64        `json:"cost_usd"`
	Models          []string        `json:"models"`
	Tools           []string        `json:"tools"`
	PolicyDecisions []string        `json:"policy_decisions"`
	Signals         []string        `json:"signals"`
	PromptHash      *string         `json:"prompt_hash"`
	SemconvVersion  string          `json:"semconv_version"`
	SDKName         *string         `json:"sdk_name"`
	SDKVersion      *string         `json:"sdk_version"`
	ContentMode     string          `json:"content_mode"`
	ContentDropped  bool            `json:"content_dropped"`
	Truncated       bool            `json:"truncated"`
	OutcomeStatus   *string         `json:"outcome_status"`
	OutcomeVerified *bool           `json:"outcome_verified"`
	HumanReviewed   bool            `json:"human_reviewed"`
	Flagged         bool            `json:"flagged"`
	Summary         json.RawMessage `json:"summary"`
	Finalized       bool            `json:"finalized"`
	ContentPurged   bool            `json:"content_purged"`
	ExpiresAt       time.Time       `json:"expires_at"`
}

const traceCols = `t.project_id, t.trace_id, t.organization_id, t.environment, t.agent_name, t.agent_version, t.session_id,
	t.release_id, t.commit_sha, t.source, t.simulation_run_id, t.scenario_id, t.root_span_id, t.root_name, t.status,
	t.started_at, t.ended_at, t.duration_ms, t.span_count, t.model_call_count, t.tool_call_count, t.error_count, t.retry_count,
	t.input_tokens, t.output_tokens, t.cost_usd::float8, t.models, t.tools, t.policy_decisions, t.signals, t.prompt_hash,
	t.semconv_version, t.sdk_name, t.sdk_version, t.content_mode, t.content_dropped, t.truncated, t.outcome_status,
	t.outcome_verified, t.human_reviewed, t.flagged, t.summary, t.finalized_at IS NOT NULL, t.content_purged, t.expires_at`

func scanTrace(r pgx.Row) (Trace, error) {
	var t Trace
	err := r.Scan(&t.ProjectID, &t.TraceID, &t.OrganizationID, &t.Environment, &t.AgentName, &t.AgentVersion, &t.SessionID,
		&t.ReleaseID, &t.CommitSHA, &t.Source, &t.SimulationRunID, &t.ScenarioID, &t.RootSpanID, &t.RootName, &t.Status,
		&t.StartedAt, &t.EndedAt, &t.DurationMS, &t.SpanCount, &t.ModelCallCount, &t.ToolCallCount, &t.ErrorCount, &t.RetryCount,
		&t.InputTokens, &t.OutputTokens, &t.CostUSD, &t.Models, &t.Tools, &t.PolicyDecisions, &t.Signals, &t.PromptHash,
		&t.SemconvVersion, &t.SDKName, &t.SDKVersion, &t.ContentMode, &t.ContentDropped, &t.Truncated, &t.OutcomeStatus,
		&t.OutcomeVerified, &t.HumanReviewed, &t.Flagged, &t.Summary, &t.Finalized, &t.ContentPurged, &t.ExpiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return t, ErrNotFound
	}
	return t, err
}

// Scope restricts queries to what a principal may see.
type Scope struct {
	OrganizationID string
	AllProjects    bool
	ProjectIDs     []string
}

// Filter is the trace explorer query.
type Filter struct {
	ProjectID       string
	Agent           string
	AgentVersion    string
	Environment     string
	Release         string
	Model           string
	Tool            string
	Status          string
	Outcome         string
	Signal          string
	PolicyDecision  string
	Source          string
	SessionID       string
	SimulationRunID string
	From, To        *time.Time
	MinDurationMS   *float64
	MaxDurationMS   *float64
	MinCostUSD      *float64
	MaxCostUSD      *float64
	HumanReviewed   *bool
	Flagged         *bool
	CursorTS        *time.Time
	CursorID        string
	Limit           int
}

type builder struct {
	where []string
	args  []any
}

func (b *builder) add(cond string, arg any) {
	b.args = append(b.args, arg)
	b.where = append(b.where, strings.ReplaceAll(cond, "?", "$"+strconv.Itoa(len(b.args))))
}

func scopeWhere(b *builder, sc Scope) {
	b.add("t.organization_id = ?", sc.OrganizationID)
	if !sc.AllProjects {
		b.add("t.project_id = ANY(?::uuid[])", sc.ProjectIDs)
	}
}

// List returns one page of traces, newest first (keyset pagination).
func (s *Store) List(ctx context.Context, sc Scope, f Filter) ([]Trace, error) {
	b := &builder{}
	scopeWhere(b, sc)
	str := map[string]string{
		"t.project_id = ?::uuid": f.ProjectID, "t.agent_name = ?": f.Agent, "t.agent_version = ?": f.AgentVersion,
		"t.environment = ?": f.Environment, "t.release_id = ?": f.Release, "? = ANY(t.models)": f.Model,
		"t.tools @> ARRAY[?]::text[]": f.Tool, "t.status = ?": f.Status, "t.outcome_status = ?": f.Outcome,
		"t.signals @> ARRAY[?]::text[]": f.Signal, "t.policy_decisions @> ARRAY[?]::text[]": f.PolicyDecision,
		"t.source = ?": f.Source, "t.session_id = ?": f.SessionID, "t.simulation_run_id = ?": f.SimulationRunID,
	}
	keys := make([]string, 0, len(str))
	for k := range str {
		keys = append(keys, k)
	}
	sort.Strings(keys) // deterministic SQL text (plan cache friendly)
	for _, k := range keys {
		if str[k] != "" {
			b.add(k, str[k])
		}
	}
	if f.From != nil {
		b.add("t.started_at >= ?", *f.From)
	}
	if f.To != nil {
		b.add("t.started_at < ?", *f.To)
	}
	if f.MinDurationMS != nil {
		b.add("t.duration_ms >= ?", *f.MinDurationMS)
	}
	if f.MaxDurationMS != nil {
		b.add("t.duration_ms <= ?", *f.MaxDurationMS)
	}
	if f.MinCostUSD != nil {
		b.add("t.cost_usd >= ?", *f.MinCostUSD)
	}
	if f.MaxCostUSD != nil {
		b.add("t.cost_usd <= ?", *f.MaxCostUSD)
	}
	if f.HumanReviewed != nil {
		b.add("t.human_reviewed = ?", *f.HumanReviewed)
	}
	if f.Flagged != nil {
		b.add("t.flagged = ?", *f.Flagged)
	}
	if f.CursorTS != nil {
		b.args = append(b.args, *f.CursorTS, f.CursorID)
		n := len(b.args)
		b.where = append(b.where, fmt.Sprintf("(t.started_at, t.trace_id) < ($%d, $%d)", n-1, n))
	}
	limit := f.Limit
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	b.args = append(b.args, limit)
	q := `SELECT ` + traceCols + ` FROM trace.trace t WHERE ` + strings.Join(b.where, " AND ") +
		` ORDER BY t.started_at DESC, t.trace_id DESC LIMIT $` + strconv.Itoa(len(b.args))
	rows, err := s.Pool.Query(ctx, q, b.args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Trace, error) { return scanTrace(r) })
}

// Get returns a trace by id within scope. projectID narrows ambiguous ids.
func (s *Store) Get(ctx context.Context, sc Scope, traceID, projectID string) (Trace, error) {
	b := &builder{}
	scopeWhere(b, sc)
	b.add("t.trace_id = ?", traceID)
	if projectID != "" {
		b.add("t.project_id = ?::uuid", projectID)
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+traceCols+` FROM trace.trace t WHERE `+strings.Join(b.where, " AND ")+` LIMIT 2`, b.args...)
	if err != nil {
		return Trace{}, err
	}
	ts, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (Trace, error) { return scanTrace(r) })
	if err != nil {
		return Trace{}, err
	}
	switch len(ts) {
	case 0:
		return Trace{}, ErrNotFound
	case 1:
		return ts[0], nil
	}
	return Trace{}, ErrAmbiguous
}

// SpanRow is a stored span.
type SpanRow struct {
	SpanID         string          `json:"span_id"`
	ParentSpanID   *string         `json:"parent_span_id"`
	Name           string          `json:"name"`
	Kind           string          `json:"kind"`
	Status         string          `json:"status"`
	StatusMessage  *string         `json:"status_message"`
	StartedAt      time.Time       `json:"started_at"`
	EndedAt        time.Time       `json:"ended_at"`
	DurationMS     float64         `json:"duration_ms"`
	ToolName       *string         `json:"tool_name"`
	ToolRisk       *string         `json:"tool_risk"`
	Model          *string         `json:"model"`
	PolicyDecision *string         `json:"policy_decision"`
	Attributes     json.RawMessage `json:"attributes"`
	Events         json.RawMessage `json:"events"`
	Content        json.RawMessage `json:"content,omitempty"`
	SemconvVersion string          `json:"semconv_version"`
}

// Spans lists the spans of a trace in start order.
func (s *Store) Spans(ctx context.Context, projectID, traceID string) ([]SpanRow, error) {
	rows, err := s.Pool.Query(ctx, `
		SELECT span_id, parent_span_id, name, kind, status, status_message, started_at, ended_at, duration_ms,
			tool_name, tool_risk, model, policy_decision, attributes, events, content, semconv_version
		FROM trace.span WHERE project_id = $1 AND trace_id = $2 ORDER BY started_at, span_id LIMIT 20000`, projectID, traceID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (SpanRow, error) {
		var sp SpanRow
		err := r.Scan(&sp.SpanID, &sp.ParentSpanID, &sp.Name, &sp.Kind, &sp.Status, &sp.StatusMessage, &sp.StartedAt, &sp.EndedAt,
			&sp.DurationMS, &sp.ToolName, &sp.ToolRisk, &sp.Model, &sp.PolicyDecision, &sp.Attributes, &sp.Events, &sp.Content, &sp.SemconvVersion)
		return sp, err
	})
}

// ModelSpans converts stored rows back into normalized spans for summaries.
func ModelSpans(traceID string, rows []SpanRow, res model.Resource) ([]model.Span, error) {
	out := make([]model.Span, 0, len(rows))
	for _, r := range rows {
		sp := model.Span{TraceID: traceID, SpanID: r.SpanID, Name: r.Name, Kind: model.Kind(r.Kind), Status: model.Status(r.Status),
			Start: r.StartedAt, End: r.EndedAt, SemconvVersion: r.SemconvVersion, Resource: res}
		if r.ParentSpanID != nil {
			sp.ParentSpanID = *r.ParentSpanID
		}
		if r.StatusMessage != nil {
			sp.StatusMessage = *r.StatusMessage
		}
		if err := json.Unmarshal(r.Attributes, &sp.Attrs); err != nil {
			return nil, fmt.Errorf("span %s attributes: %w", r.SpanID, err)
		}
		out = append(out, sp)
	}
	return out, nil
}

// Outcome is a stored outcome.
type Outcome struct {
	Status             string          `json:"status"`
	BusinessOutcome    *string         `json:"business_outcome"`
	Verified           bool            `json:"verified"`
	VerificationSource string          `json:"verification_source"`
	ClaimedStatus      *string         `json:"claimed_status"`
	Contradiction      bool            `json:"contradiction"`
	ExpectedState      json.RawMessage `json:"expected_state,omitempty"`
	ActualState        json.RawMessage `json:"actual_state,omitempty"`
	Notes              *string         `json:"notes"`
	Source             string          `json:"source"`
	RecordedBy         string          `json:"recorded_by"`
	RecordedAt         time.Time       `json:"recorded_at"`
}

// GetOutcome returns the outcome of a trace.
func (s *Store) GetOutcome(ctx context.Context, q db.Querier, projectID, traceID string) (*Outcome, error) {
	var o Outcome
	err := q.QueryRow(ctx, `
		SELECT status, business_outcome, verified, verification_source, claimed_status, contradiction, expected_state,
			actual_state, notes, source, recorded_by, recorded_at
		FROM trace.outcome WHERE project_id = $1 AND trace_id = $2`, projectID, traceID).
		Scan(&o.Status, &o.BusinessOutcome, &o.Verified, &o.VerificationSource, &o.ClaimedStatus, &o.Contradiction,
			&o.ExpectedState, &o.ActualState, &o.Notes, &o.Source, &o.RecordedBy, &o.RecordedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &o, err
}

// Flag is a stored flag.
type Flag struct {
	ID        string    `json:"id"`
	Kind      string    `json:"kind"`
	Reason    string    `json:"reason"`
	FlaggedBy string    `json:"flagged_by"`
	CreatedAt time.Time `json:"created_at"`
}

// Flags lists flags of a trace.
func (s *Store) Flags(ctx context.Context, projectID, traceID string) ([]Flag, error) {
	rows, err := s.Pool.Query(ctx, `SELECT id, kind, reason, flagged_by, created_at FROM trace.trace_flag
		WHERE project_id = $1 AND trace_id = $2 ORDER BY created_at`, projectID, traceID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Flag, error) {
		var f Flag
		err := r.Scan(&f.ID, &f.Kind, &f.Reason, &f.FlaggedBy, &f.CreatedAt)
		return f, err
	})
}

// ApplySummary writes finalized aggregates inside tx.
func ApplySummary(ctx context.Context, tx pgx.Tx, projectID, traceID string, r summary.Result, outcome *summary.Outcome) error {
	sum, err := json.Marshal(r.Summary)
	if err != nil {
		return err
	}
	var outStatus *string
	var outVerified *bool
	if outcome != nil {
		st, v := outcome.Status, outcome.Verified
		outStatus, outVerified = &st, &v
	}
	var ended *time.Time
	if !r.EndedAt.IsZero() {
		e := r.EndedAt
		ended = &e
	}
	_, err = tx.Exec(ctx, `
		UPDATE trace.trace SET status = $3, root_span_id = COALESCE(NULLIF($4, ''), root_span_id), root_name = COALESCE(NULLIF($5, ''), root_name),
			root_received = root_received OR $6, agent_name = COALESCE(NULLIF($7, ''), agent_name),
			agent_version = COALESCE(NULLIF($8, ''), agent_version), session_id = COALESCE(session_id, NULLIF($9, '')),
			ended_at = COALESCE($10, ended_at), duration_ms = $11, span_count = $12, model_call_count = $13, tool_call_count = $14,
			error_count = $15, retry_count = $16, input_tokens = $17, output_tokens = $18, cost_usd = $19, models = $20,
			tools = $21, policy_decisions = $22, signals = $23, prompt_hash = NULLIF($24, ''), semconv_version = $25,
			summary = $26, outcome_status = COALESCE($27, outcome_status), outcome_verified = COALESCE($28, outcome_verified),
			dirty = false, finalize_attempts = 0, finalized_at = COALESCE(finalized_at, now())
		WHERE project_id = $1 AND trace_id = $2`,
		projectID, traceID, string(r.Status), r.RootSpanID, r.RootName, r.RootReceived, r.AgentName, r.AgentVersion, r.SessionID,
		ended, r.DurationMS, r.SpanCount, r.ModelCalls, r.ToolCalls, r.ErrorCount, r.RetryCount, r.InputTokens, r.OutputTokens,
		r.CostUSD, r.Models, r.Tools, r.PolicyDecisions, r.Signals, r.PromptHash, r.SemconvVersion, sum, outStatus, outVerified)
	return err
}

// Stats summarizes traces for dashboards.
type Stats struct {
	Total        int64            `json:"total"`
	Errors       int64            `json:"errors"`
	ErrorRate    float64          `json:"error_rate"`
	P50MS        *float64         `json:"p50_ms"`
	P95MS        *float64         `json:"p95_ms"`
	CostUSD      float64          `json:"cost_usd"`
	ByOutcome    map[string]int64 `json:"by_outcome"`
	BySignal     map[string]int64 `json:"by_signal"`
	ByTool       map[string]int64 `json:"by_tool"`
	ByVersion    map[string]int64 `json:"by_agent_version"`
	Unverified   int64            `json:"unverified_outcomes"`
	Contradicted int64            `json:"contradictions"`
}

// Stats aggregates traces in scope and time range.
func (s *Store) Stats(ctx context.Context, sc Scope, projectID string, from, to time.Time) (Stats, error) {
	b := &builder{}
	scopeWhere(b, sc)
	if projectID != "" {
		b.add("t.project_id = ?::uuid", projectID)
	}
	b.add("t.started_at >= ?", from)
	b.add("t.started_at < ?", to)
	where := strings.Join(b.where, " AND ")
	st := Stats{ByOutcome: map[string]int64{}, BySignal: map[string]int64{}, ByTool: map[string]int64{}, ByVersion: map[string]int64{}}
	err := s.Pool.QueryRow(ctx, `
		SELECT count(*), count(*) FILTER (WHERE t.status = 'ERROR' OR t.outcome_status = 'FAILURE'),
			percentile_cont(0.5) WITHIN GROUP (ORDER BY t.duration_ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY t.duration_ms),
			COALESCE(sum(t.cost_usd), 0)::float8,
			count(*) FILTER (WHERE t.outcome_status IS NOT NULL AND NOT t.outcome_verified),
			count(*) FILTER (WHERE 'contradiction' = ANY(t.signals))
		FROM trace.trace t WHERE `+where, b.args...).Scan(&st.Total, &st.Errors, &st.P50MS, &st.P95MS, &st.CostUSD, &st.Unverified, &st.Contradicted)
	if err != nil {
		return st, err
	}
	if st.Total > 0 {
		st.ErrorRate = float64(st.Errors) / float64(st.Total)
	}
	groups := []struct {
		sql string
		m   map[string]int64
	}{
		{`SELECT COALESCE(t.outcome_status, 'NONE'), count(*) FROM trace.trace t WHERE ` + where + ` GROUP BY 1`, st.ByOutcome},
		{`SELECT sig, count(*) FROM trace.trace t, unnest(t.signals) sig WHERE ` + where + ` GROUP BY 1 ORDER BY 2 DESC LIMIT 20`, st.BySignal},
		{`SELECT tool, count(*) FROM trace.trace t, unnest(t.tools) tool WHERE ` + where + ` GROUP BY 1 ORDER BY 2 DESC LIMIT 20`, st.ByTool},
		{`SELECT COALESCE(t.agent_name || '@' || t.agent_version, 'unknown'), count(*) FROM trace.trace t WHERE ` + where + ` GROUP BY 1 ORDER BY 2 DESC LIMIT 20`, st.ByVersion},
	}
	for _, g := range groups {
		rows, err := s.Pool.Query(ctx, g.sql, b.args...)
		if err != nil {
			return st, err
		}
		for rows.Next() {
			var k string
			var n int64
			if err := rows.Scan(&k, &n); err != nil {
				rows.Close()
				return st, err
			}
			g.m[k] = n
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return st, err
		}
	}
	return st, nil
}

// Facet is a filter value and the number of recent traces carrying it.
type Facet struct {
	Value string `json:"value"`
	Count int64  `json:"count"`
}

// FacetSample bounds how many recent traces feed the filter options.
const FacetSample = 10000

// facetDims maps an API dimension to its column; array columns are unnested.
var facetDims = []struct {
	name, col string
	array     bool
}{
	{"agent", "agent_name", false}, {"agent_version", "agent_version", false}, {"environment", "environment", false},
	{"release", "release_id", false}, {"source", "source", false}, {"status", "status", false},
	{"outcome", "outcome_status", false}, {"model", "models", true}, {"tool", "tools", true},
	{"signal", "signals", true}, {"policy_decision", "policy_decisions", true},
}

// Facets returns the distinct values of each filter dimension among the most
// recent FacetSample traces since from (at most 50 per dimension, most
// frequent first). sampled reports whether the sample limit was reached.
func (s *Store) Facets(ctx context.Context, sc Scope, projectID string, from time.Time) (map[string][]Facet, bool, error) {
	b := &builder{}
	scopeWhere(b, sc)
	if projectID != "" {
		b.add("t.project_id = ?::uuid", projectID)
	}
	b.add("t.started_at >= ?", from)
	cols := make([]string, 0, len(facetDims))
	parts := make([]string, 0, len(facetDims))
	for _, d := range facetDims {
		cols = append(cols, "t."+d.col)
		if d.array {
			parts = append(parts, fmt.Sprintf(`SELECT '%s', v, count(*) FROM recent, unnest(recent.%s) v GROUP BY 2`, d.name, d.col))
		} else {
			parts = append(parts, fmt.Sprintf(`SELECT '%s', %s, count(*) FROM recent WHERE %s IS NOT NULL AND %s <> '' GROUP BY 2`, d.name, d.col, d.col, d.col))
		}
	}
	b.args = append(b.args, FacetSample)
	q := `WITH recent AS (SELECT ` + strings.Join(cols, ", ") + ` FROM trace.trace t WHERE ` + strings.Join(b.where, " AND ") +
		` ORDER BY t.started_at DESC LIMIT $` + strconv.Itoa(len(b.args)) + `), sampled AS (SELECT count(*) AS n FROM recent) ` +
		strings.Join(parts, " UNION ALL ") + ` UNION ALL SELECT '_sampled', '', n FROM sampled`
	rows, err := s.Pool.Query(ctx, q, b.args...)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	out := map[string][]Facet{}
	for _, d := range facetDims {
		out[d.name] = []Facet{}
	}
	var sampled bool
	for rows.Next() {
		var dim, v string
		var n int64
		if err := rows.Scan(&dim, &v, &n); err != nil {
			return nil, false, err
		}
		if dim == "_sampled" {
			sampled = n >= FacetSample
			continue
		}
		out[dim] = append(out[dim], Facet{Value: v, Count: n})
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	for dim, fs := range out {
		sort.Slice(fs, func(i, j int) bool {
			if fs[i].Count != fs[j].Count {
				return fs[i].Count > fs[j].Count
			}
			return fs[i].Value < fs[j].Value
		})
		if len(fs) > 50 {
			fs = fs[:50]
		}
		out[dim] = fs
	}
	return out, sampled, nil
}

// PurgeExpired deletes up to limit traces past their retention (spans,
// outcomes and flags cascade) and returns how many were deleted.
func (s *Store) PurgeExpired(ctx context.Context, limit int) (int64, error) {
	tag, err := s.Pool.Exec(ctx, `
		DELETE FROM trace.trace WHERE (project_id, trace_id) IN (
			SELECT project_id, trace_id FROM trace.trace WHERE expires_at < now() LIMIT $1)`, limit)
	return tag.RowsAffected(), err
}

// PurgeContent removes captured content older than the project's content
// retention while keeping the trace metadata.
func (s *Store) PurgeContent(ctx context.Context, limit int) (int64, error) {
	var n int64
	err := db.WithTx(ctx, s.Pool, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `
			SELECT project_id, trace_id FROM trace.trace
			WHERE content_expires_at < now() AND NOT content_purged
			ORDER BY content_expires_at LIMIT $1 FOR UPDATE SKIP LOCKED`, limit)
		if err != nil {
			return err
		}
		var projects, traces []string
		for rows.Next() {
			var p, t string
			if err := rows.Scan(&p, &t); err != nil {
				rows.Close()
				return err
			}
			projects, traces = append(projects, p), append(traces, t)
		}
		rows.Close()
		if len(traces) == 0 {
			return rows.Err()
		}
		if _, err := tx.Exec(ctx, `
			UPDATE trace.span s SET content = NULL
			FROM unnest($1::uuid[], $2::text[]) AS x(project_id, trace_id)
			WHERE s.project_id = x.project_id AND s.trace_id = x.trace_id AND s.content IS NOT NULL`, projects, traces); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `
			UPDATE trace.trace t SET content_purged = true
			FROM unnest($1::uuid[], $2::text[]) AS x(project_id, trace_id)
			WHERE t.project_id = x.project_id AND t.trace_id = x.trace_id`, projects, traces)
		n = tag.RowsAffected()
		return err
	})
	return n, err
}
