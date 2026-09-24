-- simulation schema: twins, scenarios (versioned), simulation runs, cases and
-- the tool calls the twins observed. The migrator sets search_path.

-- Twin definitions are immutable versions; a changed document is a new version.
CREATE TABLE twin_definition (
    id              uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    name            text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    version         int NOT NULL CHECK (version > 0),
    document        jsonb NOT NULL,
    spec_hash       text NOT NULL,
    tool_count      int NOT NULL,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name, version)
);

CREATE TABLE scenario (
    id              uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    name            text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,98}$'),
    agent           text,
    twin            text,
    severity        text NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    tags            text[] NOT NULL DEFAULT '{}',
    source          text NOT NULL DEFAULT 'manual',
    latest_version  int NOT NULL CHECK (latest_version > 0),
    archived        boolean NOT NULL DEFAULT false,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);
CREATE INDEX scenario_project_idx ON scenario (project_id, name);

CREATE TABLE scenario_version (
    id          uuid PRIMARY KEY,
    scenario_id uuid NOT NULL REFERENCES scenario (id) ON DELETE CASCADE,
    version     int NOT NULL CHECK (version > 0),
    document    jsonb NOT NULL,
    spec_hash   text NOT NULL,
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, version)
);

CREATE TABLE simulation_run (
    id                uuid PRIMARY KEY,
    organization_id   uuid NOT NULL,
    project_id        uuid NOT NULL,
    agent_name        text NOT NULL,
    agent_version     text NOT NULL,
    agent_version_id  uuid,
    side              text NOT NULL DEFAULT 'SINGLE' CHECK (side IN ('SINGLE', 'BASELINE', 'CANDIDATE')),
    eval_run_id       uuid,
    release_id        text,
    status            text NOT NULL CHECK (status IN ('QUEUED', 'PREPARING', 'RUNNING', 'EVALUATING',
                                                      'COMPLETED', 'FAILED', 'CANCELLED')),
    requested_by      text NOT NULL,
    cancel_requested  boolean NOT NULL DEFAULT false,
    -- Lease of the worker executing the run; an expired lease is recovered.
    lease_owner       text,
    lease_expires_at  timestamptz,
    attempts          int NOT NULL DEFAULT 0,
    case_count        int NOT NULL DEFAULT 0,
    passed            int NOT NULL DEFAULT 0,
    failed            int NOT NULL DEFAULT 0,
    errored           int NOT NULL DEFAULT 0,
    cancelled         int NOT NULL DEFAULT 0,
    critical_failures int NOT NULL DEFAULT 0,
    -- Everything needed to reproduce the run: scenario and twin versions,
    -- seeds, evaluator versions, the agent manifest hash and model kind.
    pinning           jsonb NOT NULL DEFAULT '{}',
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX simulation_run_project_idx ON simulation_run (project_id, created_at DESC, id DESC);
CREATE INDEX simulation_run_queue_idx ON simulation_run (created_at) WHERE status = 'QUEUED';
CREATE INDEX simulation_run_lease_idx ON simulation_run (lease_expires_at)
    WHERE status IN ('PREPARING', 'RUNNING', 'EVALUATING');

-- Every status change of a run (spec §86: transitions are persisted).
CREATE TABLE simulation_run_transition (
    run_id      uuid NOT NULL REFERENCES simulation_run (id) ON DELETE CASCADE,
    seq         int NOT NULL,
    from_status text,
    to_status   text NOT NULL,
    reason      text,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE simulation_case (
    id                  uuid PRIMARY KEY,
    run_id              uuid NOT NULL REFERENCES simulation_run (id) ON DELETE CASCADE,
    position            int NOT NULL,
    scenario_id         uuid NOT NULL,
    scenario_version_id uuid NOT NULL,
    scenario_name       text NOT NULL,
    severity            text NOT NULL,
    twin_definition_id  uuid NOT NULL,
    status              text NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'PASSED', 'FAILED', 'ERRORED', 'CANCELLED')),
    seed                bigint NOT NULL,
    tenant              text,
    -- SHA-256 of the case's capability token (the agent's credential for the
    -- twin); NULL once the case is closed, so late calls are refused.
    token_hash          bytea UNIQUE,
    twin_state          jsonb,
    call_count          int NOT NULL DEFAULT 0,
    agent_result        jsonb,
    trace_id            text,
    verdict             jsonb,
    results             jsonb,
    state_diff          jsonb,
    reason              text,
    error               text,
    latency_ms          double precision,
    -- The verified outcome posted to the trace service (tool_twin_state).
    outcome_status      text NOT NULL DEFAULT 'none' CHECK (outcome_status IN ('none', 'pending', 'posted', 'failed')),
    outcome_attempts    int NOT NULL DEFAULT 0,
    outcome_next_at     timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, position)
);
CREATE INDEX simulation_case_outcome_idx ON simulation_case (outcome_next_at) WHERE outcome_status = 'pending';

-- The steps the twin observed: tool calls (the twin's record) and retrievals.
CREATE TABLE simulation_step (
    case_id    uuid NOT NULL REFERENCES simulation_case (id) ON DELETE CASCADE,
    seq        int NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('tool_call', 'retrieval')),
    tool       text,
    record     jsonb NOT NULL,
    latency_ms double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, seq)
);

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
