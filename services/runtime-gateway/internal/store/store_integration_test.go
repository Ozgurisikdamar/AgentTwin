package store_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/migrations"
)

var t0 = time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)

func migrated(t *testing.T) (*store.Store, *pgxpool.Pool, *db.Migrator) {
	t.Helper()
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t), Schema: migrations.Schema, MaxConns: 5})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	migs, err := db.LoadMigrations(migrations.FS, ".")
	if err != nil {
		t.Fatal(err)
	}
	m := &db.Migrator{Pool: pool, Schema: migrations.Schema, Migrations: migs}
	if _, err := m.Up(ctx); err != nil {
		t.Fatalf("up: %v", err)
	}
	return store.New(pool), pool, m
}

func restricted(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "23001"
}

func inTx(t *testing.T, pool *pgxpool.Pool, fn func(pgx.Tx) error) error {
	t.Helper()
	return pgx.BeginFunc(context.Background(), pool, fn)
}

// The schema can be rolled back and applied again.
func TestMigrationsRollBackAndReapply(t *testing.T) {
	_, pool, m := migrated(t)
	ctx := context.Background()
	if n, err := m.Down(ctx, len(m.Migrations)); err != nil || n != len(m.Migrations) {
		t.Fatalf("down: %d, %v", n, err)
	}
	var tables int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM pg_tables WHERE schemaname = $1 AND tablename <> 'schema_migrations'`,
		migrations.Schema).Scan(&tables); err != nil || tables != 0 {
		t.Fatalf("tables left after rollback: %d %v", tables, err)
	}
	if n, err := m.Up(ctx); err != nil || n != len(m.Migrations) {
		t.Fatalf("up again: %d, %v", n, err)
	}
}

func decision(id string, outcome string) store.Decision {
	effect := "allow"
	e := &effect
	if outcome == store.OutcomeReplayed {
		e = nil
	}
	return store.Decision{
		ID: id, Tool: "refund_payment", Risk: "WRITE_IRREVERSIBLE", Environment: "production", Subject: "apikey:k",
		ActionHash: strings.Repeat("a", 64), Arguments: map[string]any{"amount": 40.0}, Summary: "refund_payment(amount=40)",
		Effect: e, Outcome: outcome, Message: "ok", CreatedAt: t0,
	}
}

func TestADecisionIsCompletedOnceAndNeverChanges(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	id := ids.New()
	if err := s.InsertDecision(ctx, pool, sc, decision(id, store.OutcomeForwarded)); err != nil {
		t.Fatal(err)
	}
	status := 200
	if err := s.CompleteDecision(ctx, pool, id, store.OutcomeExecuted, &status, "", 12, t0.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := s.CompleteDecision(ctx, pool, id, store.OutcomeFailed, nil, "TIMEOUT", 12, t0); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("a completed decision cannot complete again: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_decision SET message = 'changed' WHERE id = $1`, id); !restricted(err) {
		t.Fatalf("a completed decision cannot change: %v", err)
	}
	other := ids.New()
	if err := s.InsertDecision(ctx, pool, sc, decision(other, store.OutcomeForwarded)); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_decision SET outcome = 'executed', completed_at = now(), rule = 'x' WHERE id = $1`, other); !restricted(err) {
		t.Fatalf("completing may not change what was decided: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_decision SET outcome = 'denied', completed_at = now() WHERE id = $1`, other); !restricted(err) {
		t.Fatalf("a forwarded call ends executed or failed: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_decision SET outcome = 'executed' WHERE id = $1`, other); !restricted(err) {
		t.Fatalf("a completed call says when: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_decision SET outcome = 'failed', completed_at = now() WHERE id = $1`, id); !restricted(err) {
		t.Fatalf("a completed decision is not completed again: %v", err)
	}
	// Completing a call may not change anything that was decided.
	for col, val := range map[string]string{
		"id": "gen_random_uuid()", "organization_id": "gen_random_uuid()", "project_id": "gen_random_uuid()",
		"tool": "'other'", "risk": "'READ'", "agent": "'other'", "agent_version": "'9.9.9'", "environment": "'staging'",
		"subject": "'apikey:other'", "trace_id": "repeat('b', 32)", "action_hash": "repeat('f', 64)",
		"arguments": `'{"amount": 1500}'`, "summary": "'other'", "effect": "'deny'", "policy_name": "'other'",
		"policy_version_id": "gen_random_uuid()", "rule": "'other'", "message": "'other'", "decisions": `'[{}]'`,
		"fail_mode_applied": "true", "approval_id": "gen_random_uuid()", "idempotency_key": "'other'",
		"limits": `'{"timeoutMs": 1}'`, "created_at": "now()",
	} {
		if _, err := pool.Exec(ctx, `UPDATE policy_decision SET outcome = 'executed', completed_at = now(), `+col+` = `+val+
			` WHERE id = $1`, other); !restricted(err) {
			t.Fatalf("completing may not change %s: %v", col, err)
		}
	}
	if _, err := pool.Exec(ctx, `DELETE FROM policy_decision WHERE id = $1`, id); !restricted(err) {
		t.Fatalf("decisions are kept: %v", err)
	}
	if err := s.InsertDecision(ctx, pool, sc, decision(ids.New(), store.OutcomeReplayed)); err != nil {
		t.Fatalf("a replay has no effect: %v", err)
	}
	bad := decision(ids.New(), store.OutcomeReplayed)
	effect := "allow"
	bad.Effect = &effect
	if err := s.InsertDecision(ctx, pool, sc, bad); err == nil {
		t.Fatal("a replay must not carry an effect")
	}
	got, err := s.Decision(ctx, sc, id)
	if err != nil || got.Outcome != store.OutcomeExecuted || *got.UpstreamStatus != 200 || *got.LatencyMs != 12 ||
		got.Arguments["amount"] != 40.0 || string(got.Decisions) != "[]" || string(got.Limits) != "null" {
		t.Fatalf("decision %+v %v", got, err)
	}
	if _, err := s.Decision(ctx, store.Scope{OrgID: ids.New(), ProjectID: sc.ProjectID}, id); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("another organization must not read it: %v", err)
	}
	if list, err := s.Decisions(ctx, store.Scope{OrgID: ids.New(), ProjectID: sc.ProjectID}, store.DecisionFilter{}, nil, 10); err != nil || len(list) != 0 {
		t.Fatalf("another organization lists none: %+v %v", list, err)
	}
	all, err := s.Decisions(ctx, sc, store.DecisionFilter{}, nil, 10)
	if err != nil || len(all) != 3 {
		t.Fatalf("all %d %v", len(all), err)
	}
	page, _ := s.Decisions(ctx, sc, store.DecisionFilter{}, nil, 2)
	last := page[len(page)-1]
	rest, err := s.Decisions(ctx, sc, store.DecisionFilter{}, &httpx.Cursor{TS: last.CreatedAt, ID: last.ID}, 10)
	if err != nil || len(page) != 2 || len(rest) != 1 || rest[0].ID != all[2].ID {
		t.Fatalf("pages %d %d %v", len(page), len(rest), err)
	}
	for f, want := range map[store.DecisionFilter]int{
		{Outcome: store.OutcomeReplayed}: 1, {Outcome: store.OutcomeExecuted}: 1, {Tool: "other"}: 0,
		{Tool: "refund_payment"}: 3, {Effect: "allow"}: 2, {TraceID: strings.Repeat("0", 32)}: 0,
	} {
		if list, err := s.Decisions(ctx, sc, f, nil, 10); err != nil || len(list) != want {
			t.Fatalf("filter %+v: %d %v", f, len(list), err)
		}
	}
}

func TestPolicyVersionsAreImmutable(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	p := store.Policy{ID: ids.New(), Name: "refund-limits", Tool: "refund_payment", CreatedBy: "user:u", CreatedAt: t0}
	v := store.Version{ID: ids.New(), PolicyID: p.ID, Version: 1, Document: "doc", Spec: json.RawMessage(`{"a":1}`),
		SpecHash: strings.Repeat("b", 64), CreatedBy: "user:u", CreatedAt: t0}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.CreatePolicy(ctx, tx, sc, p, v) }); err != nil {
		t.Fatal(err)
	}
	dup := p
	dup.ID = ids.New()
	dupV := v
	dupV.ID, dupV.PolicyID = ids.New(), dup.ID
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.CreatePolicy(ctx, tx, sc, dup, dupV) }); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("a name is unique in the project: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE policy_version SET document = 'x' WHERE id = $1`, v.ID); !restricted(err) {
		t.Fatalf("a version cannot change: %v", err)
	}
	if _, err := pool.Exec(ctx, `DELETE FROM policy_version WHERE id = $1`, v.ID); !restricted(err) {
		t.Fatalf("a version cannot be deleted: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Activate(ctx, tx, p.ID, &v.ID, "user:admin", t0) }); err != nil {
		t.Fatal(err)
	}
	active, err := s.ActiveVersions(ctx, pool, sc, "refund_payment")
	if err != nil || len(active) != 1 || active[0].PolicyName != "refund-limits" || active[0].Version != 1 {
		t.Fatalf("active %+v %v", active, err)
	}
	if active, err := s.ActiveVersions(ctx, pool, store.Scope{OrgID: sc.OrgID, ProjectID: ids.New()}, "refund_payment"); err != nil || len(active) != 0 {
		t.Fatalf("another project is not guarded: %+v %v", active, err)
	}
	for tool, want := range map[string]int{"": 1, "refund_payment": 1, "lookup_order": 0} {
		if list, err := s.Policies(ctx, sc, tool); err != nil || len(list) != want {
			t.Fatalf("policies for %q: %d %v", tool, len(list), err)
		}
	}
	got, err := s.Policy(ctx, pool, sc, p.ID, false)
	if err != nil || got.Active == nil || got.Active.Version != 1 || *got.ActivatedBy != "user:admin" {
		t.Fatalf("policy %+v %v", got, err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Activate(ctx, tx, p.ID, nil, "user:admin", t0) }); err != nil {
		t.Fatal(err)
	}
	got, _ = s.Policy(ctx, pool, sc, p.ID, false)
	if got.Active != nil || got.ActivatedBy != nil || got.ActivatedAt != nil {
		t.Fatalf("deactivated %+v", got)
	}
	if active, _ := s.ActiveVersions(ctx, pool, sc, "refund_payment"); len(active) != 0 {
		t.Fatalf("no active version: %+v", active)
	}
}

func approval(id string) store.Approval {
	return store.Approval{
		ID: id, Tool: "refund_payment", Risk: "WRITE_IRREVERSIBLE", Environment: "production",
		ActionHash: strings.Repeat("c", 64), Arguments: map[string]any{"amount": 150.0}, Summary: "refund_payment(amount=150)",
		PolicyName: "refund-limits", Rule: "approval-above-100", Reason: "needs approval", DecisionID: ids.New(),
		RequestedBy: "apikey:k", ExpiresAt: t0.Add(15 * time.Minute), CreatedAt: t0,
	}
}

func TestApprovalsOnlyMoveForward(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	a := approval(ids.New())
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.InsertApproval(ctx, tx, sc, a) }); err != nil {
		t.Fatal(err)
	}
	second := approval(ids.New())
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.InsertApproval(ctx, tx, sc, second) }); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("one open request per action: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		open, err := s.OpenApproval(ctx, tx, sc, a.ActionHash, t0)
		if err != nil || open.ID != a.ID {
			t.Fatalf("open %+v %v", open, err)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		_, err := s.OpenApproval(ctx, tx, store.Scope{OrgID: ids.New(), ProjectID: sc.ProjectID}, a.ActionHash, t0)
		return err
	}); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("another organization has no open request for the action: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.SetToken(ctx, tx, a.ID, strings.Repeat("d", 64), t0, t0) }); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("a pending request has no token: %v", err)
	}
	for col, val := range map[string]string{
		"id": "gen_random_uuid()", "organization_id": "gen_random_uuid()", "project_id": "gen_random_uuid()",
		"tool": "'other'", "risk": "'READ'", "agent": "'other'", "agent_version": "'9.9.9'", "environment": "'staging'",
		"trace_id": "repeat('b', 32)", "action_hash": "repeat('f', 64)", "arguments": `'{"amount": 1500}'`,
		"summary": "'other'", "policy_name": "'other'", "policy_version_id": "gen_random_uuid()", "rule": "'other'",
		"reason": "'other'", "decision_id": "gen_random_uuid()", "requested_by": "'apikey:other'",
		"expires_at": "now() + interval '1 day'", "created_at": "now()",
	} {
		if _, err := pool.Exec(ctx, `UPDATE approval_request SET `+col+` = `+val+` WHERE id = $1`, a.ID); !restricted(err) {
			t.Fatalf("what was asked cannot change (%s): %v", col, err)
		}
	}
	if err := s.SetStatus(ctx, pool, a.ID, "USED", t0); !restricted(err) {
		t.Fatalf("PENDING cannot become USED: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Decide(ctx, tx, a.ID, "APPROVED", "user:r", "fine", t0) }); err != nil {
		t.Fatal(err)
	}
	if err := s.SetStatus(ctx, pool, a.ID, "PENDING", t0); !restricted(err) {
		t.Fatalf("APPROVED cannot go back to PENDING: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Decide(ctx, tx, a.ID, "DENIED", "user:r", "", t0) }); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("a decided request is not decided again: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		return s.SetToken(ctx, tx, a.ID, strings.Repeat("d", 64), t0.Add(time.Minute), t0)
	}); err != nil {
		t.Fatal(err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		got, err := s.ApprovalByToken(ctx, tx, strings.Repeat("d", 64))
		if err != nil || got.ID != a.ID || got.Status != "APPROVED" || *got.DecidedBy != "user:r" || *got.DecisionReason != "fine" {
			t.Fatalf("by token %+v %v", got, err)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	usedBy := ids.New()
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Use(ctx, tx, a.ID, usedBy, t0) }); err != nil {
		t.Fatal(err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.Use(ctx, tx, a.ID, usedBy, t0) }); !errors.Is(err, store.ErrConflict) {
		t.Fatalf("an approval is used once: %v", err)
	}
	if err := s.SetStatus(ctx, pool, a.ID, "EXPIRED", t0); !restricted(err) {
		t.Fatalf("a used approval never changes: %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE approval_request SET decision_reason = 'changed' WHERE id = $1`, a.ID); !restricted(err) {
		t.Fatalf("a closed request never changes: %v", err)
	}
	if _, err := pool.Exec(ctx, `DELETE FROM approval_request WHERE id = $1`, a.ID); !restricted(err) {
		t.Fatalf("requests are kept: %v", err)
	}
	// A new request for the same action once the first is no longer pending.
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.InsertApproval(ctx, tx, sc, second) }); err != nil {
		t.Fatalf("a closed request frees the action: %v", err)
	}
	// An expired pending request is closed when looked up, freeing the action.
	third := approval(ids.New())
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		if _, err := s.OpenApproval(ctx, tx, sc, second.ActionHash, second.ExpiresAt); !errors.Is(err, store.ErrNotFound) {
			t.Fatalf("expired: %v", err)
		}
		return s.InsertApproval(ctx, tx, sc, third)
	}); err != nil {
		t.Fatal(err)
	}
	for status, want := range map[string]int{"": 3, "PENDING": 1, "USED": 1, "EXPIRED": 1, "APPROVED": 0} {
		list, err := s.Approvals(ctx, sc, store.ApprovalFilter{Status: status}, t0, nil, 10)
		if err != nil || len(list) != want {
			t.Fatalf("status %q: %d %v", status, len(list), err)
		}
	}
	if list, _ := s.Approvals(ctx, sc, store.ApprovalFilter{Status: "PENDING"}, third.ExpiresAt, nil, 10); len(list) != 0 {
		t.Fatalf("a pending request past its expiry is not pending: %+v", list)
	}
	if list, _ := s.Approvals(ctx, sc, store.ApprovalFilter{Status: "EXPIRED"}, third.ExpiresAt, nil, 10); len(list) != 2 {
		t.Fatalf("it is expired: %+v", list)
	}
	att := store.Attempt{ID: ids.New(), ApprovalID: a.ID, Result: "mismatch", ActionHash: strings.Repeat("e", 64),
		Arguments: map[string]any{"amount": 1500.0}, Subject: "apikey:k", CreatedAt: t0}
	if err := s.InsertAttempt(ctx, pool, sc, att); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE approval_attempt SET result = 'executed'`); !restricted(err) {
		t.Fatalf("attempts are append-only: %v", err)
	}
	atts, err := s.Attempts(ctx, a.ID)
	if err != nil || len(atts) != 1 || atts[0].Arguments["amount"] != 1500.0 {
		t.Fatalf("attempts %+v %v", atts, err)
	}
}

func TestIdempotencyRecords(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	h1, h2 := strings.Repeat("1", 64), strings.Repeat("2", 64)
	d1 := ids.New()
	lock := func(at time.Time) *store.Idem {
		var got *store.Idem
		if err := inTx(t, pool, func(tx pgx.Tx) error {
			var err error
			got, err = s.LockIdem(ctx, tx, sc, "refund_payment", "k", at)
			return err
		}); err != nil {
			t.Fatal(err)
		}
		return got
	}
	if lock(t0) != nil {
		t.Fatal("a new key has no record")
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.ClaimIdem(ctx, tx, sc, "refund_payment", "k", h1, d1, t0) }); err != nil {
		t.Fatal(err)
	}
	if r := lock(t0); r == nil || r.State != "IN_PROGRESS" || r.ActionHash != h1 || r.DecisionID != d1 {
		t.Fatalf("claimed %+v", r)
	}
	status := 200
	if err := s.FinishIdem(ctx, pool, sc, "refund_payment", "k", d1, "COMPLETED", &status, []byte(`{"result":1}`), "application/json", t0); err != nil {
		t.Fatal(err)
	}
	r := lock(t0)
	if r.State != "COMPLETED" || *r.ResponseStatus != 200 || string(r.ResponseBody) != `{"result":1}` || *r.ResponseContentType != "application/json" {
		t.Fatalf("completed %+v", r)
	}
	// Finishing for a decision that no longer holds the key changes nothing.
	if err := s.FinishIdem(ctx, pool, sc, "refund_payment", "k", ids.New(), "UNKNOWN", nil, nil, "", t0); err != nil {
		t.Fatal(err)
	}
	if r := lock(t0); r.State != "COMPLETED" {
		t.Fatalf("another decision's finish: %+v", r)
	}
	// An unknown outcome keeps no response.
	d2 := ids.New()
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.ClaimIdem(ctx, tx, sc, "refund_payment", "k", h1, d2, t0) }); err != nil {
		t.Fatal(err)
	}
	if err := s.FinishIdem(ctx, pool, sc, "refund_payment", "k", d2, "UNKNOWN", &status, []byte("x"), "text/plain", t0); err != nil {
		t.Fatal(err)
	}
	if r := lock(t0); r.State != "UNKNOWN" || r.ResponseBody != nil || r.ResponseContentType != nil {
		t.Fatalf("unknown %+v", r)
	}
	// After the retention the key is new again, for any action.
	if lock(t0.Add(store.IdemRetention)) != nil {
		t.Fatal("an expired record is not live")
	}
	later := t0.Add(store.IdemRetention + time.Hour)
	if err := inTx(t, pool, func(tx pgx.Tx) error { return s.ClaimIdem(ctx, tx, sc, "refund_payment", "k", h2, ids.New(), later) }); err != nil {
		t.Fatal(err)
	}
	if r := lock(later); r == nil || r.ActionHash != h2 {
		t.Fatalf("reclaimed after expiry: %+v", r)
	}
	if r := lock(later.Add(store.IdemRetention - time.Minute)); r == nil {
		t.Fatal("a reclaimed record lives from its reclaim")
	}
}

// Expired idempotency records and old trace calls are deleted; live ones
// stay.
func TestExpiredRecordsArePurged(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	now := t0.Add(store.TraceCallRetention + time.Hour)
	hash := strings.Repeat("1", 64)
	for key, at := range map[string]time.Time{"expired": t0, "live": now.Add(-time.Hour)} {
		if err := inTx(t, pool, func(tx pgx.Tx) error { return s.ClaimIdem(ctx, tx, sc, "refund_payment", key, hash, ids.New(), at) }); err != nil {
			t.Fatal(err)
		}
	}
	for trace, at := range map[string]time.Time{strings.Repeat("a", 32): t0, strings.Repeat("b", 32): now.Add(-time.Hour)} {
		if err := s.RecordTraceCall(ctx, pool, sc, trace, "refund_payment", hash, at); err != nil {
			t.Fatal(err)
		}
	}
	n, err := s.PurgeExpired(ctx, now)
	if err != nil || n != 2 {
		t.Fatalf("purged %d %v", n, err)
	}
	var keys, traces string
	if err := pool.QueryRow(ctx, `SELECT string_agg(key, ',') FROM idempotency_record`).Scan(&keys); err != nil || keys != "live" {
		t.Fatalf("kept keys %q %v", keys, err)
	}
	if err := pool.QueryRow(ctx, `SELECT string_agg(trace_id, ',') FROM trace_tool_call`).Scan(&traces); err != nil || traces != strings.Repeat("b", 32) {
		t.Fatalf("kept traces %q %v", traces, err)
	}
	if n, err := s.PurgeExpired(ctx, now); err != nil || n != 0 {
		t.Fatalf("nothing left to purge: %d %v", n, err)
	}
}

func TestTraceCallsCountOtherActions(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	trace := strings.Repeat("f", 32)
	h1, h2 := strings.Repeat("1", 64), strings.Repeat("2", 64)
	count := func(hash string) int {
		n, err := s.TraceCalls(ctx, pool, sc, trace, "refund_payment", hash)
		if err != nil {
			t.Fatal(err)
		}
		return n
	}
	if count(h1) != 0 {
		t.Fatal("no call yet")
	}
	for range 2 {
		if err := s.RecordTraceCall(ctx, pool, sc, trace, "refund_payment", h1, t0); err != nil {
			t.Fatal(err)
		}
	}
	if count(h1) != 0 || count(h2) != 1 {
		t.Fatalf("a retry of the same action is not another call: %d %d", count(h1), count(h2))
	}
	if n, _ := s.TraceCalls(ctx, pool, sc, trace, "send_email", h2); n != 0 {
		t.Fatal("per tool")
	}
	if n, _ := s.TraceCalls(ctx, pool, store.Scope{OrgID: sc.OrgID, ProjectID: ids.New()}, trace, "refund_payment", h2); n != 0 {
		t.Fatal("per project")
	}
	if err := s.RecordTraceCall(ctx, pool, sc, "", "refund_payment", h2, t0); err != nil {
		t.Fatal(err)
	}
	if n, _ := s.TraceCalls(ctx, pool, sc, "", "refund_payment", h1); n != 0 {
		t.Fatal("no trace, no count")
	}
}

func TestEndpoints(t *testing.T) {
	s, pool, _ := migrated(t)
	ctx := context.Background()
	sc := store.Scope{OrgID: ids.New(), ProjectID: ids.New()}
	e := store.Endpoint{Tool: "refund_payment", Kind: "http", URL: "http://demo-tools:8091/tools/refund_payment",
		Risk: "WRITE_IRREVERSIBLE", TimeoutMs: 5000, Idempotency: "required", ForwardHeaders: []string{"X-AgentTwin-Tenant"}, UpdatedBy: "user:a"}
	var created bool
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		var err error
		_, created, err = s.PutEndpoint(ctx, tx, sc, e, t0)
		return err
	}); err != nil || !created {
		t.Fatalf("created %v %v", created, err)
	}
	e.TimeoutMs, e.ForwardHeaders = 2000, nil
	var got store.Endpoint
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		var err error
		got, created, err = s.PutEndpoint(ctx, tx, sc, e, t0.Add(time.Hour))
		return err
	}); err != nil || created || got.TimeoutMs != 2000 || !got.CreatedAt.Equal(t0) || !got.UpdatedAt.Equal(t0.Add(time.Hour)) || got.ForwardHeaders == nil {
		t.Fatalf("replaced %+v %v %v", got, created, err)
	}
	list, err := s.Endpoints(ctx, sc)
	if err != nil || len(list) != 1 {
		t.Fatalf("list %+v %v", list, err)
	}
	if _, err := s.Endpoint(ctx, pool, store.Scope{OrgID: ids.New(), ProjectID: sc.ProjectID}, "refund_payment"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("scoped: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		_, err := s.DeleteEndpoint(ctx, tx, store.Scope{OrgID: ids.New(), ProjectID: sc.ProjectID}, "refund_payment")
		return err
	}); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("another organization cannot delete it: %v", err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		_, err := s.DeleteEndpoint(ctx, tx, sc, "refund_payment")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := inTx(t, pool, func(tx pgx.Tx) error {
		_, err := s.DeleteEndpoint(ctx, tx, sc, "refund_payment")
		return err
	}); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("deleted twice: %v", err)
	}
}
