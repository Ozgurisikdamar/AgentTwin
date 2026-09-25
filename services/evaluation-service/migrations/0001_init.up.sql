-- evaluation schema: datasets (versioned collections of scenarios), evaluation
-- runs (a baseline version against a candidate version over one pinned
-- suite), their per-case comparisons, the judge verdict cache, human reviews
-- and judge calibrations. The migrator sets search_path.

CREATE TABLE dataset (
    id              uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    name            text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,98}$'),
    description     text,
    owner           text,
    tags            text[] NOT NULL DEFAULT '{}',
    latest_version  int NOT NULL CHECK (latest_version > 0),
    archived        boolean NOT NULL DEFAULT false,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);
CREATE INDEX dataset_project_idx ON dataset (project_id, name);

-- Versions are immutable snapshots: a change to the cases is a new version.
CREATE TABLE dataset_version (
    id          uuid PRIMARY KEY,
    dataset_id  uuid NOT NULL REFERENCES dataset (id) ON DELETE CASCADE,
    version     int NOT NULL CHECK (version > 0),
    cases       jsonb NOT NULL,
    case_count  int NOT NULL CHECK (case_count >= 0),
    note        text,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, version)
);

CREATE TABLE eval_run (
    id                    uuid PRIMARY KEY,
    organization_id       uuid NOT NULL,
    project_id            uuid NOT NULL,
    agent_name            text NOT NULL,
    baseline_version      text NOT NULL,
    candidate_version     text NOT NULL,
    dataset_id            uuid REFERENCES dataset (id),
    dataset_version       int,
    -- What was asked for: scenario names and/or tags (the dataset's names
    -- are resolved into it when the run is requested).
    selection             jsonb NOT NULL,
    seed                  bigint,
    release_id            text,
    status                text NOT NULL CHECK (status IN ('QUEUED', 'PREPARING', 'RUNNING', 'EVALUATING',
                                                          'COMPLETED', 'FAILED', 'CANCELLED')),
    requested_by          text NOT NULL,
    cancel_requested      boolean NOT NULL DEFAULT false,
    lease_owner           text,
    lease_expires_at      timestamptz,
    attempts              int NOT NULL DEFAULT 0,
    -- While RUNNING no worker holds the run: it waits for its simulations,
    -- checked when due (a completion event makes it due at once) until the
    -- deadline.
    next_check_at         timestamptz,
    wait_deadline         timestamptz,
    -- The two simulation runs (one pinned suite, ADR-0023) once requested.
    baseline_run_id       uuid,
    candidate_run_id      uuid,
    -- The pinned seed and suite, as the simulation service answered them.
    pinning               jsonb NOT NULL DEFAULT '{}',
    judge                 jsonb,
    budget                jsonb,
    summary               jsonb,
    case_count            int NOT NULL DEFAULT 0,
    new_critical_failures int NOT NULL DEFAULT 0,
    regressed             int NOT NULL DEFAULT 0,
    improved              int NOT NULL DEFAULT 0,
    unchanged             int NOT NULL DEFAULT 0,
    incomplete            int NOT NULL DEFAULT 0,
    error                 text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    started_at            timestamptz,
    finished_at           timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_run_project_idx ON eval_run (project_id, created_at DESC, id DESC);
CREATE INDEX eval_run_queue_idx ON eval_run (created_at) WHERE status = 'QUEUED';
CREATE INDEX eval_run_lease_idx ON eval_run (lease_expires_at)
    WHERE status IN ('PREPARING', 'RUNNING', 'EVALUATING');
CREATE INDEX eval_run_waiting_idx ON eval_run (next_check_at) WHERE status = 'RUNNING';
CREATE INDEX eval_run_simulation_idx ON eval_run (baseline_run_id);
CREATE INDEX eval_run_dataset_idx ON eval_run (dataset_id, created_at DESC) WHERE dataset_id IS NOT NULL;

-- Every status change of an evaluation run (spec §86).
CREATE TABLE eval_run_transition (
    eval_run_id uuid NOT NULL REFERENCES eval_run (id) ON DELETE CASCADE,
    seq         int NOT NULL,
    from_status text,
    to_status   text NOT NULL,
    reason      text,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_run_id, seq)
);

-- One case of an evaluation: what each side did (the simulation's case
-- detail with the judged semantic results, kept so a review can re-grade it
-- without the simulation service) and how the two compare.
CREATE TABLE eval_case_result (
    eval_run_id       uuid NOT NULL REFERENCES eval_run (id) ON DELETE CASCADE,
    position          int NOT NULL,
    scenario_name     text NOT NULL,
    severity          text NOT NULL,
    tags              text[] NOT NULL DEFAULT '{}',
    classification    text NOT NULL CHECK (classification IN ('NEW_CRITICAL_FAILURE', 'REGRESSED', 'IMPROVED',
                                                              'UNCHANGED', 'INCOMPLETE')),
    baseline_case_id  uuid,
    candidate_case_id uuid,
    sides             jsonb NOT NULL,
    comparison        jsonb NOT NULL,
    reviewed          boolean NOT NULL DEFAULT false,
    -- Expectations a person should look at ("BASELINE:<id>"): the judge
    -- could not grade them, or graded a critical one uncalibrated.
    needs_review      text[] NOT NULL DEFAULT '{}',
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_run_id, position),
    UNIQUE (eval_run_id, scenario_name)
);

-- Judge verdicts by everything that shaped them (ADR-0022): reused, never
-- approximated.
CREATE TABLE judgment (
    cache_key  text PRIMARY KEY CHECK (cache_key ~ '^[a-f0-9]{64}$'),
    verdict    jsonb NOT NULL,
    judge      jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A person's verdict on one expectation of one side; the latest one for an
-- expectation replaces the evaluator's result in the comparison. Kept whole
-- (append-only) and announced to the audit log.
CREATE TABLE human_review (
    id              uuid PRIMARY KEY,
    eval_run_id     uuid NOT NULL REFERENCES eval_run (id) ON DELETE CASCADE,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    scenario_name   text NOT NULL,
    side            text NOT NULL CHECK (side IN ('BASELINE', 'CANDIDATE')),
    expectation_id  text NOT NULL,
    original_status text NOT NULL,
    original_label  text,
    status          text NOT NULL CHECK (status IN ('PASS', 'FAIL')),
    note            text NOT NULL,
    reviewer        text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX human_review_case_idx ON human_review (eval_run_id, scenario_name, created_at);

-- A judge measured against labels people gave (spec §16.4).
CREATE TABLE judge_calibration (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    criterion        text NOT NULL,
    status           text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    examples         jsonb NOT NULL,
    example_count    int NOT NULL CHECK (example_count > 0),
    examples_sha256  text NOT NULL CHECK (examples_sha256 ~ '^[a-f0-9]{64}$'),
    judge            jsonb,
    metrics          jsonb,
    calibrated       boolean,
    reason           text,
    disagreements    jsonb,
    error            text,
    requested_by     text NOT NULL,
    lease_owner      text,
    lease_expires_at timestamptz,
    attempts         int NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX judge_calibration_project_idx ON judge_calibration (project_id, criterion, created_at DESC);
CREATE INDEX eval_case_result_review_idx ON eval_case_result (eval_run_id) WHERE cardinality(needs_review) > 0;
CREATE INDEX judge_calibration_queue_idx ON judge_calibration (created_at) WHERE status = 'QUEUED';

CREATE TABLE outbox (
    id           uuid PRIMARY KEY,
    event_type   text NOT NULL,
    envelope     jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts     int NOT NULL DEFAULT 0,
    last_error   text
);
CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;

CREATE TABLE processed_event (
    consumer     text NOT NULL,
    event_id     uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
