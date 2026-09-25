// Package finalizer turns settled traces into their deterministic summary and
// publishes trace.ingested.v1 exactly once per trace through the outbox.
//
// Spans of one agent run arrive in several OTLP batches, and the root span
// usually arrives last. A trace is finalized once its root span was received
// and no span arrived for Settle (or, for traces whose root never arrives,
// after IncompleteAfter). Late spans re-finalize the stored summary but do not
// re-publish the ingestion event.
package finalizer

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/summary"
)

// Producer is the event producer name.
const Producer = "trace-service"

// MaxAttempts bounds retries of a trace that fails to finalize; it is retried
// again only when new spans arrive.
const MaxAttempts = 10

// Finalizer settles traces.
type Finalizer struct {
	Pool            *pgxpool.Pool
	Store           *store.Store
	Log             *slog.Logger
	Settle          time.Duration
	IncompleteAfter time.Duration
	Interval        time.Duration
	BatchSize       int
	Pricing         summary.Pricing
	// Observe receives the number of finalized traces per tick (metrics).
	Observe func(n int)
	// OnError is called once per trace that failed to finalize (metrics).
	OnError func()
}

// Run polls until ctx is cancelled.
func (f *Finalizer) Run(ctx context.Context) {
	interval := f.Interval
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		n, err := f.Tick(ctx)
		if err != nil && ctx.Err() == nil && f.Log != nil {
			f.Log.Warn("finalizer tick failed", "error", err.Error())
		}
		if f.Observe != nil {
			f.Observe(n)
		}
		if n > 0 {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

type candidate struct {
	projectID, traceID string
}

// Tick finalizes up to BatchSize settled traces and returns how many.
func (f *Finalizer) Tick(ctx context.Context) (int, error) {
	batch := f.BatchSize
	if batch <= 0 {
		batch = 50
	}
	rows, err := f.Pool.Query(ctx, `
		SELECT project_id, trace_id FROM trace.trace
		WHERE dirty AND updated_at < now() - make_interval(secs => $1)
			AND (root_received OR updated_at < now() - make_interval(secs => $2))
			AND finalize_attempts < $4
		ORDER BY updated_at LIMIT $3`, f.Settle.Seconds(), f.IncompleteAfter.Seconds(), batch, MaxAttempts)
	if err != nil {
		return 0, err
	}
	var cands []candidate
	for rows.Next() {
		var c candidate
		if err := rows.Scan(&c.projectID, &c.traceID); err != nil {
			rows.Close()
			return 0, err
		}
		cands = append(cands, c)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	done := 0
	var firstErr error
	for _, c := range cands {
		ok, err := f.finalize(ctx, c)
		if err != nil {
			// One bad trace must not block the others: log it, push it to the
			// back of the queue (updated_at drives candidate order and the
			// settle delay) and continue.
			if firstErr == nil {
				firstErr = fmt.Errorf("finalize %s: %w", c.traceID, err)
			}
			if f.Log != nil {
				f.Log.Error("finalize trace failed", "trace_id", c.traceID, "project_id", c.projectID, "error", err.Error())
			}
			if f.OnError != nil {
				f.OnError()
			}
			if ctx.Err() == nil {
				_, _ = f.Pool.Exec(ctx, `UPDATE trace.trace SET updated_at = now(), finalize_attempts = finalize_attempts + 1
					WHERE project_id = $1 AND trace_id = $2`, c.projectID, c.traceID)
			}
			continue
		}
		if ok {
			done++
		}
	}
	return done, firstErr
}

func (f *Finalizer) finalize(ctx context.Context, c candidate) (bool, error) {
	finalized := false
	err := db.WithTx(ctx, f.Pool, func(tx pgx.Tx) error {
		var (
			orgID, env, source              string
			agent, version, release, commit *string
			simRun, scenario                *string
			emitted                         *time.Time
			dirty                           bool
		)
		// Lock the row; a concurrent finalizer replica skips it.
		err := tx.QueryRow(ctx, `
			SELECT organization_id, environment, source, agent_name, agent_version, release_id, commit_sha,
				simulation_run_id, scenario_id, ingested_emitted_at, dirty
			FROM trace.trace WHERE project_id = $1 AND trace_id = $2 FOR UPDATE SKIP LOCKED`, c.projectID, c.traceID).
			Scan(&orgID, &env, &source, &agent, &version, &release, &commit, &simRun, &scenario, &emitted, &dirty)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil // taken by another replica or deleted
		}
		if err != nil {
			return err
		}
		if !dirty {
			return nil // already clean
		}
		rows, err := f.Store.Spans(ctx, c.projectID, c.traceID)
		if err != nil {
			return err
		}
		res := model.Resource{Environment: env, AgentName: deref(agent), AgentVersion: deref(version)}
		spans, err := store.ModelSpans(c.traceID, rows, res)
		if err != nil {
			return err
		}
		apiOutcome, err := f.Store.GetOutcome(ctx, tx, c.projectID, c.traceID)
		if err != nil {
			return err
		}
		var api *summary.Outcome
		if apiOutcome != nil && apiOutcome.Source == "api" {
			api = &summary.Outcome{Status: apiOutcome.Status, Verified: apiOutcome.Verified,
				VerificationSource: apiOutcome.VerificationSource, Contradiction: apiOutcome.Contradiction}
		}
		r := summary.Build(spans, api, f.Pricing)
		effective := r.SpanOutcome
		if api != nil {
			effective = api
		}
		if err := store.ApplySummary(ctx, tx, c.projectID, c.traceID, r, effective); err != nil {
			return err
		}
		// Outcomes reported on spans are stored (API/human outcomes win).
		if r.SpanOutcome != nil && api == nil {
			if err := f.recordSpanOutcome(ctx, tx, orgID, c, r.SpanOutcome); err != nil {
				return err
			}
		}
		if emitted == nil {
			payload := map[string]any{
				"trace_id":          c.traceID,
				"agent":             firstNonEmpty(r.AgentName, deref(agent), "unknown"),
				"agent_version":     nilIfEmpty(firstNonEmpty(r.AgentVersion, deref(version))),
				"environment":       env,
				"source":            source,
				"started_at":        r.StartedAt.UTC().Format(time.RFC3339Nano),
				"signals":           r.Signals,
				"summary":           r.Summary,
				"observed_tools":    r.ObservedTools,
				"release_id":        release,
				"commit_sha":        commit,
				"simulation_run_id": simRun,
				"scenario_id":       scenario,
			}
			env, err := events.New("trace.ingested.v1", Producer, orgID, c.projectID, c.traceID, "", payload)
			if err != nil {
				return err
			}
			if err := events.WriteOutbox(ctx, tx, "trace", env); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE trace.trace SET ingested_emitted_at = now() WHERE project_id = $1 AND trace_id = $2`, c.projectID, c.traceID); err != nil {
				return err
			}
		}
		finalized = true
		return nil
	})
	return finalized, err
}

func (f *Finalizer) recordSpanOutcome(ctx context.Context, tx pgx.Tx, orgID string, c candidate, o *summary.Outcome) error {
	var emittedAt *time.Time
	var prevStatus string
	var prevVerified bool
	err := tx.QueryRow(ctx, `SELECT emitted_at, status, verified FROM trace.outcome WHERE project_id = $1 AND trace_id = $2`, c.projectID, c.traceID).
		Scan(&emittedAt, &prevStatus, &prevVerified)
	exists := err == nil
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if exists && emittedAt != nil && prevStatus == o.Status && prevVerified == o.Verified {
		return nil
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO trace.outcome (project_id, trace_id, status, business_outcome, verified, verification_source, claimed_status,
			contradiction, source, recorded_by, emitted_at)
		VALUES ($1, $2, $3, NULLIF($4, ''), $5, $6, NULLIF($7, ''), $8, 'span', 'sdk', now())
		ON CONFLICT (project_id, trace_id) DO UPDATE SET status = EXCLUDED.status, business_outcome = EXCLUDED.business_outcome,
			verified = EXCLUDED.verified, verification_source = EXCLUDED.verification_source, claimed_status = EXCLUDED.claimed_status,
			contradiction = EXCLUDED.contradiction, recorded_at = now(), emitted_at = now()
		WHERE trace.outcome.source = 'span'`,
		c.projectID, c.traceID, o.Status, o.BusinessOutcome, o.Verified, o.VerificationSource, o.Claimed, o.Contradiction); err != nil {
		return err
	}
	env, err := events.New("trace.outcome_recorded.v1", Producer, orgID, c.projectID, c.traceID, "", map[string]any{
		"trace_id": c.traceID, "status": o.Status, "business_outcome": nilIfEmpty(o.BusinessOutcome), "verified": o.Verified,
		"verification_source": o.VerificationSource, "contradiction": o.Contradiction, "source": "span",
	})
	if err != nil {
		return err
	}
	return events.WriteOutbox(ctx, tx, "trace", env)
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if s != "" {
			return s
		}
	}
	return ""
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
