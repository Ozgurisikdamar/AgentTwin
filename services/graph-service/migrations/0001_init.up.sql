-- graph schema: components, the relationships between them and the evidence
-- for each relationship (spec §19). Tables are created in the graph schema
-- (the migrator sets search_path).

CREATE TABLE component (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('AGENT', 'AGENT_VERSION', 'PROMPT', 'MODEL', 'TOOL',
                         'MCP_SERVER', 'HTTP_API', 'SERVICE', 'DATABASE', 'QUEUE', 'DATASET', 'POLICY',
                         'SCENARIO', 'EVALUATOR', 'EXTERNAL_SYSTEM', 'RETRIEVAL_SOURCE')),
    key              text NOT NULL CHECK (length(key) BETWEEN 1 AND 300),
    label            text NOT NULL CHECK (length(label) BETWEEN 1 AND 300),
    attributes       jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL,
    -- The organization is part of the key: a project id named by a caller of
    -- another organization can never collide with (or block) this one's.
    UNIQUE (organization_id, project_id, kind, key)
);

-- One edge per (from, type, to); its sources and confidence are derived from
-- its evidence and kept in step by the store.
CREATE TABLE dependency_edge (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    from_id          uuid NOT NULL REFERENCES component (id) ON DELETE CASCADE,
    to_id            uuid NOT NULL REFERENCES component (id) ON DELETE CASCADE,
    type             text NOT NULL CHECK (type IN ('USES', 'CALLS', 'READS', 'WRITES', 'PUBLISHES', 'CONSUMES',
                         'GUARDED_BY', 'EVALUATED_BY', 'TESTED_BY', 'DEPENDS_ON', 'RETRIEVES_FROM',
                         'CAN_MUTATE', 'VERSION_OF')),
    sources          text[] NOT NULL DEFAULT '{}',
    confidence       numeric(3, 2) NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 1),
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL,
    UNIQUE (from_id, type, to_id)
);
CREATE INDEX dependency_edge_to ON dependency_edge (to_id, type);
CREATE INDEX dependency_edge_scope ON dependency_edge (organization_id, project_id);

-- Why an edge exists: one row per source and reference (a manifest version,
-- an imported document, a scenario, a policy, a manual mapping, production
-- traffic). Observed evidence counts observations instead of one row per trace.
CREATE TABLE edge_evidence (
    edge_id          uuid NOT NULL REFERENCES dependency_edge (id) ON DELETE CASCADE,
    source           text NOT NULL CHECK (source IN ('MANIFEST', 'OPENAPI', 'MCP', 'OBSERVED', 'MANUAL',
                         'SCENARIO', 'INFERRED')),
    source_ref       text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 300),
    confidence       numeric(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    observations     bigint NOT NULL DEFAULT 1 CHECK (observations > 0),
    detail           jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL,
    PRIMARY KEY (edge_id, source, source_ref)
);
CREATE INDEX edge_evidence_ref ON edge_evidence (source, source_ref);

CREATE TABLE processed_event (
    consumer      text NOT NULL,
    event_id      uuid NOT NULL,
    processed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);

CREATE TABLE outbox (
    id            uuid PRIMARY KEY,
    event_type    text NOT NULL,
    envelope      jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz,
    attempts      int NOT NULL DEFAULT 0,
    last_error    text
);
CREATE INDEX outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;
