-- Regression mining (Phase 6, ADR-0032): production failures grouped by
-- known fingerprint or similarity, reviewed by people and promoted to
-- regression tests. Nothing here holds conversation content: occurrences
-- keep structured features only.
--
-- pgvector is installed in public, which the service's search_path reaches;
-- another service may install it too, so installing is serialized.
SELECT pg_advisory_xact_lock(hashtext('agenttwin-extension-vector'));
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- A failure seen in production, once or many times.
CREATE TABLE regression_group (
    id                      uuid PRIMARY KEY,
    organization_id         uuid NOT NULL,
    project_id              uuid NOT NULL,
    agent_name              text NOT NULL,
    -- The fingerprint the group was created with (later ones map to it
    -- through regression_fingerprint).
    fingerprint             text NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{32}$'),
    title                   text NOT NULL,
    status                  text NOT NULL CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'PROMOTED', 'FIXED',
                                                            'DISMISSED', 'REOPENED')),
    -- What the rules suggest; a person may change the label and severity.
    suggested_taxonomy      text NOT NULL,
    suggested_severity      text NOT NULL CHECK (suggested_severity IN ('critical', 'high', 'medium', 'low')),
    taxonomy                text NOT NULL,
    secondary               text[] NOT NULL DEFAULT '{}',
    severity                text NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    severity_reason         text,
    -- Who last set the label or the severity (NULL: they are the rules').
    triaged_by              text,
    tags                    text[] NOT NULL DEFAULT '{}',
    evidence                text[] NOT NULL DEFAULT '{}',
    -- The component the failure points at (the failing tool).
    component               text,
    assignee                text,
    representative_trace_id text NOT NULL CHECK (representative_trace_id ~ '^[a-f0-9]{32}$'),
    occurrence_count        int NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    first_seen              timestamptz NOT NULL,
    last_seen               timestamptz NOT NULL,
    versions                text[] NOT NULL DEFAULT '{}',
    environments            text[] NOT NULL DEFAULT '{}',
    -- A group merged into another one (it keeps its history; new failures
    -- of its fingerprints join the other).
    merged_into             uuid REFERENCES regression_group (id),
    -- The test it became (ADR-0032 "promotion").
    scenario_id             uuid,
    scenario_name           text,
    dataset_id              uuid REFERENCES dataset (id),
    dataset_version         int,
    promoted_by             text,
    promoted_at             timestamptz,
    fixed_version           text,
    fixed_eval_run_id       uuid,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, organization_id, project_id),
    CHECK (merged_into IS NULL OR merged_into <> id),
    CHECK ((status IN ('PROMOTED', 'FIXED')) <= (scenario_name IS NOT NULL))
);
-- The inbox: a project's live groups, most recently seen first.
CREATE INDEX regression_group_inbox_idx ON regression_group (project_id, last_seen DESC, id DESC)
    WHERE merged_into IS NULL;
-- An evaluation run's candidate passing a promoted group's scenario fixes it.
CREATE INDEX regression_group_scenario_idx ON regression_group (project_id, agent_name, scenario_name)
    WHERE scenario_name IS NOT NULL;

-- Known failures: every fingerprint maps to one group of the project's
-- agent (a merge re-points the merged group's fingerprints).
CREATE TABLE regression_fingerprint (
    project_id  uuid NOT NULL,
    agent_name  text NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{32}$'),
    group_id    uuid NOT NULL REFERENCES regression_group (id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, agent_name, fingerprint)
);

-- One production trace that failed: its structured features, why it is a
-- candidate, and the vector its features' sentence embeds to (stored with
-- the model that produced it; only vectors of one model are compared). The
-- foreign key over the group's tenancy makes an occurrence of another tenant
-- impossible, so a neighbour search never crosses tenants.
CREATE TABLE regression_occurrence (
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    trace_id        text NOT NULL CHECK (trace_id ~ '^[a-f0-9]{32}$'),
    group_id        uuid NOT NULL,
    agent_name      text NOT NULL,
    agent_version   text,
    environment     text,
    started_at      timestamptz NOT NULL,
    fingerprint     text NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{32}$'),
    -- What the rules say about this trace (the group shows its
    -- representative's).
    title           text NOT NULL,
    component       text,
    taxonomy        text NOT NULL,
    secondary       text[] NOT NULL DEFAULT '{}',
    severity        text NOT NULL CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    severity_reason text NOT NULL,
    evidence        text[] NOT NULL DEFAULT '{}',
    reasons         text[] NOT NULL CHECK (cardinality(reasons) > 0),
    observation     jsonb NOT NULL,
    features        jsonb NOT NULL,
    join_kind       text NOT NULL CHECK (join_kind IN ('exact', 'similar', 'new')),
    join_reason     text NOT NULL,
    similarity      double precision,
    model           text NOT NULL CHECK (model ~ '^[A-Za-z0-9._:/-]{1,200}$'),
    dims            int NOT NULL CHECK (dims BETWEEN 16 AND 16000),
    -- NULL: the features embed to nothing to compare.
    embedding       vector CHECK (embedding IS NULL OR vector_dims(embedding) = dims),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, trace_id),
    FOREIGN KEY (group_id, organization_id, project_id)
        REFERENCES regression_group (id, organization_id, project_id) ON DELETE CASCADE
);
CREATE INDEX regression_occurrence_group_idx ON regression_occurrence (group_id, started_at DESC, trace_id);
-- Nearest neighbours are searched among one project's agent and model
-- (exact search: a project's failures are few enough; an ANN index is an
-- upgrade for when they are not, ADR-0001).
CREATE INDEX regression_occurrence_neighbour_idx ON regression_occurrence (project_id, agent_name, model, dims);

-- Flags people put on traces (trace.flagged.v1), kept so a flag that
-- arrives before the trace is finalized is not lost.
CREATE TABLE regression_flag (
    event_id   uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    trace_id   text NOT NULL CHECK (trace_id ~ '^[a-f0-9]{32}$'),
    kind       text NOT NULL CHECK (kind IN ('incident', 'negative_feedback', 'manual')),
    reason     text NOT NULL,
    flagged_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX regression_flag_trace_idx ON regression_flag (project_id, trace_id, created_at);

-- What happened to a group: created, status changes, triage, promotion,
-- merges (spec §121: every transition with its actor and reason).
CREATE TABLE regression_event (
    group_id    uuid NOT NULL REFERENCES regression_group (id) ON DELETE CASCADE,
    seq         int NOT NULL,
    action      text NOT NULL CHECK (action IN ('created', 'confirm', 'dismiss', 'reopen', 'promote', 'fixed',
                                                'merge', 'merged', 'triage', 'assign')),
    from_status text,
    to_status   text,
    actor       text NOT NULL,
    reason      text,
    detail      jsonb NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, seq)
);
