-- trace schema: normalized agent traces, spans, outcomes and flags.
-- Tables are created in the trace schema (the migrator sets search_path).

CREATE TABLE trace (
    project_id          uuid NOT NULL,
    trace_id            text NOT NULL CHECK (trace_id ~ '^[a-f0-9]{32}$'),
    organization_id     uuid NOT NULL,
    environment         text NOT NULL DEFAULT 'unknown',
    agent_name          text,
    agent_version       text,
    session_id          text,
    release_id          text,
    commit_sha          text,
    source              text NOT NULL DEFAULT 'production'
                        CHECK (source IN ('production', 'simulation', 'replay', 'eval', 'test')),
    simulation_run_id   text,
    scenario_id         text,
    root_span_id        text,
    root_name           text,
    status              text NOT NULL DEFAULT 'UNSET' CHECK (status IN ('OK', 'ERROR', 'UNSET')),
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    duration_ms         double precision,
    span_count          int NOT NULL DEFAULT 0,
    model_call_count    int NOT NULL DEFAULT 0,
    tool_call_count     int NOT NULL DEFAULT 0,
    error_count         int NOT NULL DEFAULT 0,
    retry_count         int NOT NULL DEFAULT 0,
    input_tokens        bigint NOT NULL DEFAULT 0,
    output_tokens       bigint NOT NULL DEFAULT 0,
    cost_usd            numeric(14, 6),
    models              text[] NOT NULL DEFAULT '{}',
    tools               text[] NOT NULL DEFAULT '{}',
    policy_decisions    text[] NOT NULL DEFAULT '{}',
    signals             text[] NOT NULL DEFAULT '{}',
    prompt_hash         text,
    semconv_version     text NOT NULL DEFAULT 'none',
    sdk_name            text,
    sdk_version         text,
    content_mode        text NOT NULL CHECK (content_mode IN ('off', 'redacted', 'full')),
    content_dropped     boolean NOT NULL DEFAULT false,
    truncated           boolean NOT NULL DEFAULT false,
    outcome_status      text CHECK (outcome_status IN ('SUCCESS', 'PARTIAL', 'FAILURE', 'UNKNOWN')),
    outcome_verified    boolean,
    human_reviewed      boolean NOT NULL DEFAULT false,
    flagged             boolean NOT NULL DEFAULT false,
    summary             jsonb NOT NULL DEFAULT '{}',
    root_received       boolean NOT NULL DEFAULT false,
    -- Settling state: ingestion bumps revision and marks the trace dirty; the
    -- finalizer recomputes the summary once no span arrived for a short while.
    revision            bigint NOT NULL DEFAULT 1,
    dirty               boolean NOT NULL DEFAULT true,
    -- Failed finalization attempts since the last ingested span; traces that
    -- keep failing stop being retried until new data arrives.
    finalize_attempts   integer NOT NULL DEFAULT 0,
    finalized_at        timestamptz,
    ingested_emitted_at timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    content_expires_at  timestamptz NOT NULL,
    content_purged      boolean NOT NULL DEFAULT false,
    PRIMARY KEY (project_id, trace_id)
);

-- Explorer list: newest first with keyset pagination.
CREATE INDEX trace_list_idx ON trace (project_id, started_at DESC, trace_id DESC);
CREATE INDEX trace_org_trace_idx ON trace (organization_id, trace_id);
CREATE INDEX trace_agent_idx ON trace (project_id, agent_name, agent_version, started_at DESC);
CREATE INDEX trace_tools_idx ON trace USING gin (tools);
CREATE INDEX trace_signals_idx ON trace USING gin (signals);
CREATE INDEX trace_policy_idx ON trace USING gin (policy_decisions);
CREATE INDEX trace_dirty_idx ON trace (updated_at) WHERE dirty;
CREATE INDEX trace_expiry_idx ON trace (expires_at);
CREATE INDEX trace_content_expiry_idx ON trace (content_expires_at) WHERE NOT content_purged;

CREATE TABLE span (
    project_id      uuid NOT NULL,
    trace_id        text NOT NULL,
    span_id         text NOT NULL CHECK (span_id ~ '^[a-f0-9]{16}$'),
    parent_span_id  text CHECK (parent_span_id IS NULL OR parent_span_id ~ '^[a-f0-9]{16}$'),
    name            text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('agent', 'model', 'tool', 'retrieval', 'policy', 'outcome', 'http', 'mcp', 'other')),
    otel_kind       smallint NOT NULL DEFAULT 0,
    status          text NOT NULL CHECK (status IN ('OK', 'ERROR', 'UNSET')),
    status_message  text,
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz NOT NULL,
    duration_ms     double precision NOT NULL,
    -- Extracted columns for filters and summaries; the full normalized view is in attributes.
    tool_name       text,
    tool_risk       text,
    model           text,
    policy_decision text,
    attributes      jsonb NOT NULL DEFAULT '{}',
    events          jsonb NOT NULL DEFAULT '[]',
    content         jsonb,
    semconv_version text NOT NULL DEFAULT 'none',
    received_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, trace_id, span_id),
    FOREIGN KEY (project_id, trace_id) REFERENCES trace (project_id, trace_id) ON DELETE CASCADE
);
CREATE INDEX span_trace_order_idx ON span (project_id, trace_id, started_at);

CREATE TABLE outcome (
    project_id          uuid NOT NULL,
    trace_id            text NOT NULL,
    status              text NOT NULL CHECK (status IN ('SUCCESS', 'PARTIAL', 'FAILURE', 'UNKNOWN')),
    business_outcome    text,
    verified            boolean NOT NULL,
    verification_source text NOT NULL CHECK (verification_source IN (
        'state_assertion', 'tool_twin_state', 'tool_result', 'external_callback', 'human_review', 'semantic_judge', 'unavailable')),
    claimed_status      text CHECK (claimed_status IN ('SUCCESS', 'PARTIAL', 'FAILURE', 'UNKNOWN')),
    contradiction       boolean NOT NULL DEFAULT false,
    expected_state      jsonb,
    actual_state        jsonb,
    notes               text,
    source              text NOT NULL CHECK (source IN ('span', 'api')),
    recorded_by         text NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    emitted_at          timestamptz,
    PRIMARY KEY (project_id, trace_id),
    FOREIGN KEY (project_id, trace_id) REFERENCES trace (project_id, trace_id) ON DELETE CASCADE,
    CHECK (NOT verified OR verification_source <> 'unavailable')
);

CREATE TABLE trace_flag (
    id          uuid PRIMARY KEY,
    project_id  uuid NOT NULL,
    trace_id    text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('incident', 'negative_feedback', 'manual')),
    reason      text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    flagged_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, trace_id) REFERENCES trace (project_id, trace_id) ON DELETE CASCADE
);
CREATE INDEX trace_flag_trace_idx ON trace_flag (project_id, trace_id);

CREATE TABLE outbox (
    id            uuid PRIMARY KEY,
    event_type    text NOT NULL,
    envelope      jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz,
    attempts      int NOT NULL DEFAULT 0,
    last_error    text
);
CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
