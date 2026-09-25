-- control schema: identity, tenancy, projects, agents, tools, API keys, audit.
-- search_path is set to the control schema by the migrator.

CREATE TABLE organization (
    id          uuid PRIMARY KEY,
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id            uuid PRIMARY KEY,
    email         text NOT NULL UNIQUE CHECK (length(email) <= 320),
    display_name  text NOT NULL,
    oidc_subject  text UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz
);

CREATE TABLE membership (
    organization_id uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('OWNER','ADMIN','ENGINEER','REVIEWER','VIEWER')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);
CREATE INDEX membership_user_idx ON membership (user_id);

CREATE TABLE project (
    id                      uuid PRIMARY KEY,
    organization_id         uuid NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    slug                    text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name                    text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    description             text NOT NULL DEFAULT '',
    content_mode            text NOT NULL DEFAULT 'off' CHECK (content_mode IN ('off','redacted','full')),
    store_prompt_text       boolean NOT NULL DEFAULT true,
    trace_retention_days    int NOT NULL DEFAULT 30 CHECK (trace_retention_days BETWEEN 1 AND 3650),
    content_retention_days  int NOT NULL DEFAULT 7 CHECK (content_retention_days BETWEEN 1 AND 3650),
    artifact_retention_days int NOT NULL DEFAULT 30 CHECK (artifact_retention_days BETWEEN 1 AND 3650),
    gate_policy             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by              text NOT NULL,
    updated_by              text NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, slug),
    UNIQUE (organization_id, id)
);

CREATE TABLE environment (
    id             uuid PRIMARY KEY,
    project_id     uuid NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name           text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    is_production  boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE api_key (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    name             text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    prefix           text NOT NULL UNIQUE CHECK (prefix ~ '^[a-z0-9]{8}$'),
    secret_hash      text NOT NULL,
    scopes           text[] NOT NULL CHECK (cardinality(scopes) > 0),
    created_by       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz,
    revoked_at       timestamptz,
    revoked_by       text,
    last_used_at     timestamptz,
    rotated_from     uuid REFERENCES api_key(id),
    FOREIGN KEY (organization_id, project_id) REFERENCES project(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX api_key_project_idx ON api_key (project_id);

CREATE TABLE agent (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    name             text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    description      text NOT NULL DEFAULT '',
    created_by       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name),
    FOREIGN KEY (organization_id, project_id) REFERENCES project(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX agent_org_idx ON agent (organization_id);

CREATE TABLE prompt_version (
    id          uuid PRIMARY KEY,
    agent_id    uuid NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
    sha256      text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    content     text,
    size_bytes  int NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, sha256)
);

CREATE TABLE tool (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    name             text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    description      text NOT NULL DEFAULT '',
    current_risk     text NOT NULL CHECK (current_risk IN ('READ','WRITE_REVERSIBLE','WRITE_IRREVERSIBLE','EXECUTE','ADMIN')),
    source           text NOT NULL CHECK (source IN ('MANIFEST','OPENAPI','MCP','MANUAL')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name),
    FOREIGN KEY (organization_id, project_id) REFERENCES project(organization_id, id) ON DELETE CASCADE
);

CREATE TABLE tool_version (
    id                  uuid PRIMARY KEY,
    tool_id             uuid NOT NULL REFERENCES tool(id) ON DELETE CASCADE,
    version             int NOT NULL CHECK (version > 0),
    definition_sha256   text NOT NULL,
    risk                text NOT NULL CHECK (risk IN ('READ','WRITE_REVERSIBLE','WRITE_IRREVERSIBLE','EXECUTE','ADMIN')),
    risk_dimensions     jsonb,
    compensating_action jsonb,
    approval_condition  text,
    input_schema        jsonb,
    description         text NOT NULL DEFAULT '',
    source              text NOT NULL,
    source_ref          text,
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tool_id, version),
    UNIQUE (tool_id, definition_sha256)
);

CREATE TABLE agent_version (
    id                 uuid PRIMARY KEY,
    agent_id           uuid NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
    version            text NOT NULL,
    manifest           jsonb NOT NULL,
    manifest_sha256    text NOT NULL CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
    prompt_version_id  uuid REFERENCES prompt_version(id),
    model_provider     text NOT NULL,
    model_name         text NOT NULL,
    model_params       jsonb NOT NULL DEFAULT '{}'::jsonb,
    runtime_endpoint   text,
    commit_sha         text CHECK (commit_sha IS NULL OR commit_sha ~ '^[a-f0-9]{7,64}$'),
    repo_url           text,
    branch             text,
    created_by         text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, version)
);
CREATE INDEX agent_version_agent_idx ON agent_version (agent_id, created_at DESC);

CREATE TABLE agent_version_tool (
    agent_version_id  uuid NOT NULL REFERENCES agent_version(id) ON DELETE CASCADE,
    tool_version_id   uuid NOT NULL REFERENCES tool_version(id),
    PRIMARY KEY (agent_version_id, tool_version_id)
);

-- Append-only, hash-chained audit log (per organization).
CREATE TABLE audit_event (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid,
    actor            text NOT NULL,
    action           text NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_]+$'),
    resource_type    text NOT NULL,
    resource_id      text NOT NULL,
    occurred_at      timestamptz NOT NULL,
    request_id       text,
    before_hash      text,
    after_hash       text,
    reason           text,
    metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_service   text NOT NULL,
    source_event_id  uuid UNIQUE,
    prev_hash        text,
    entry_hash       text NOT NULL,
    seq              bigint GENERATED ALWAYS AS IDENTITY,
    recorded_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_event_org_seq_idx ON audit_event (organization_id, seq DESC);
CREATE INDEX audit_event_resource_idx ON audit_event (organization_id, resource_type, resource_id);

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END;
$$;
CREATE TRIGGER audit_event_append_only BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Edge idempotency for mutating requests (Idempotency-Key header).
CREATE TABLE idempotency_record (
    principal     text NOT NULL,
    key           text NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
    method        text NOT NULL,
    path          text NOT NULL,
    request_hash  text NOT NULL,
    status        int,
    response      bytea,
    content_type  text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, key)
);
CREATE INDEX idempotency_record_created_idx ON idempotency_record (created_at);

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
CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
