-- Baseline/candidate pairs of evaluation runs (Phase 3). The evaluation
-- service asks for both runs at once; they are created together, pin the
-- same scenario versions, twin definitions and seeds, and differ only in the
-- agent version. One pair per evaluation run: a repeated request answers the
-- existing pair (the request hash tells a repeat from a conflicting request).
CREATE TABLE simulation_pair (
    eval_run_id      uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    -- Deferred: the pair row is written first, to claim the evaluation run
    -- before its two runs exist (concurrent requests wait on the key).
    baseline_run_id  uuid NOT NULL UNIQUE REFERENCES simulation_run (id) DEFERRABLE INITIALLY DEFERRED,
    candidate_run_id uuid NOT NULL UNIQUE REFERENCES simulation_run (id) DEFERRABLE INITIALLY DEFERRED,
    request_sha256   text NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    requested_by     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (baseline_run_id <> candidate_run_id)
);
CREATE INDEX simulation_pair_project_idx ON simulation_pair (project_id, created_at DESC);
-- Runs of an evaluation are listed by it (GET /api/v1/simulations?eval_run_id=).
CREATE INDEX simulation_run_eval_idx ON simulation_run (eval_run_id) WHERE eval_run_id IS NOT NULL;
