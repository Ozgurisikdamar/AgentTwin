-- runtime schema (ADR-0033): the tools the gateway may call, the policies
-- that guard them and their immutable versions, every decision it takes,
-- approval requests and what was attempted with them, idempotency records
-- and the calls forwarded per trace. Tables are created in the runtime
-- schema (the migrator sets search_path).

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;

-- A tool the gateway may forward to, per project. Registering one is
-- refused unless its host is in the egress allowlist.
CREATE TABLE tool_endpoint (
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    tool             text NOT NULL CHECK (tool ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    kind             text NOT NULL CHECK (kind IN ('http', 'mcp')),
    url              text NOT NULL CHECK (length(url) BETWEEN 1 AND 2000),
    risk             text NOT NULL CHECK (risk IN ('READ', 'WRITE_REVERSIBLE', 'WRITE_IRREVERSIBLE', 'EXECUTE', 'ADMIN')),
    timeout_ms       int NOT NULL CHECK (timeout_ms BETWEEN 100 AND 60000),
    idempotency      text NOT NULL CHECK (idempotency IN ('required', 'optional')),
    forward_headers  text[] NOT NULL DEFAULT '{}',
    updated_by       text NOT NULL,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    PRIMARY KEY (organization_id, project_id, tool)
);

-- A named policy that guards one tool. Its versions are immutable; one of
-- them may be active.
CREATE TABLE policy (
    id                 uuid PRIMARY KEY,
    organization_id    uuid NOT NULL,
    project_id         uuid NOT NULL,
    name               text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    tool               text NOT NULL,
    description        text NOT NULL DEFAULT '',
    latest_version     int NOT NULL CHECK (latest_version > 0),
    active_version_id  uuid,
    activated_by       text,
    activated_at       timestamptz,
    created_by         text NOT NULL,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    UNIQUE (organization_id, project_id, name)
);
CREATE INDEX policy_tool ON policy (organization_id, project_id, tool) WHERE active_version_id IS NOT NULL;

CREATE TABLE policy_version (
    id          uuid PRIMARY KEY,
    policy_id   uuid NOT NULL REFERENCES policy (id),
    version     int NOT NULL CHECK (version > 0),
    -- The document as its author wrote it, and as read (defaults filled).
    document    text NOT NULL CHECK (length(document) BETWEEN 1 AND 262144),
    spec        jsonb NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
    spec_hash   text NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL,
    UNIQUE (policy_id, version)
);
CREATE TRIGGER policy_version_immutable BEFORE UPDATE OR DELETE ON policy_version
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
ALTER TABLE policy ADD FOREIGN KEY (active_version_id) REFERENCES policy_version (id);

-- Every decision the gateway takes. A forwarded call is recorded before it
-- leaves and completed once, when the tool answers or fails; nothing else
-- ever changes.
CREATE TABLE policy_decision (
    id                 uuid PRIMARY KEY,
    organization_id    uuid NOT NULL,
    project_id         uuid NOT NULL,
    tool               text NOT NULL,
    risk               text NOT NULL,
    agent              text NOT NULL DEFAULT '',
    agent_version      text NOT NULL DEFAULT '',
    environment        text NOT NULL,
    subject            text NOT NULL,
    trace_id           text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    action_hash        text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    arguments          jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
    summary            text NOT NULL,
    effect             text CHECK (effect IN ('allow', 'allow_with_limits', 'require_approval', 'deny')),
    outcome            text NOT NULL CHECK (outcome IN ('forwarded', 'executed', 'failed', 'replayed', 'denied',
                           'approval_required', 'approval_refused')),
    policy_name        text,
    policy_version_id  uuid,
    rule               text,
    message            text NOT NULL,
    decisions          jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(decisions) = 'array'),
    fail_mode_applied  boolean NOT NULL DEFAULT false,
    approval_id        uuid,
    idempotency_key    text,
    limits             jsonb,
    upstream_status    int,
    error_code         text,
    latency_ms         int,
    created_at         timestamptz NOT NULL,
    completed_at       timestamptz,
    -- A replayed call is decided by the call it replays, not by a policy.
    CHECK ((outcome = 'replayed') = (effect IS NULL))
);
CREATE INDEX policy_decision_recent ON policy_decision (organization_id, project_id, created_at DESC, id DESC);
CREATE INDEX policy_decision_trace ON policy_decision (organization_id, project_id, trace_id) WHERE trace_id IS NOT NULL;

CREATE FUNCTION policy_decision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.outcome <> 'forwarded' OR NEW.outcome NOT IN ('executed', 'failed') OR NEW.completed_at IS NULL
       OR (NEW.id, NEW.organization_id, NEW.project_id, NEW.tool, NEW.risk, NEW.agent, NEW.agent_version,
           NEW.environment, NEW.subject, NEW.trace_id, NEW.action_hash, NEW.arguments, NEW.summary, NEW.effect,
           NEW.policy_name, NEW.policy_version_id, NEW.rule, NEW.message, NEW.decisions, NEW.fail_mode_applied,
           NEW.approval_id, NEW.idempotency_key, NEW.limits, NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.organization_id, OLD.project_id, OLD.tool, OLD.risk, OLD.agent, OLD.agent_version,
           OLD.environment, OLD.subject, OLD.trace_id, OLD.action_hash, OLD.arguments, OLD.summary, OLD.effect,
           OLD.policy_name, OLD.policy_version_id, OLD.rule, OLD.message, OLD.decisions, OLD.fail_mode_applied,
           OLD.approval_id, OLD.idempotency_key, OLD.limits, OLD.created_at) THEN
        RAISE EXCEPTION 'policy_decision % is a record: only a forwarded call is completed, once', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER policy_decision_once BEFORE UPDATE ON policy_decision
    FOR EACH ROW EXECUTE FUNCTION policy_decision_guard();
CREATE TRIGGER policy_decision_kept BEFORE DELETE ON policy_decision
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- A person's decision on one exact action. The token is stored as a hash.
CREATE TABLE approval_request (
    id                 uuid PRIMARY KEY,
    organization_id    uuid NOT NULL,
    project_id         uuid NOT NULL,
    tool               text NOT NULL,
    risk               text NOT NULL,
    agent              text NOT NULL DEFAULT '',
    agent_version      text NOT NULL DEFAULT '',
    environment        text NOT NULL,
    trace_id           text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    action_hash        text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    arguments          jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
    summary            text NOT NULL,
    policy_name        text NOT NULL,
    policy_version_id  uuid,
    rule               text NOT NULL,
    reason             text NOT NULL,
    decision_id        uuid NOT NULL,
    status             text NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'USED')),
    requested_by       text NOT NULL,
    expires_at         timestamptz NOT NULL,
    decided_by         text,
    decided_at         timestamptz,
    decision_reason    text CHECK (length(decision_reason) <= 2000),
    token_hash         text CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    token_expires_at   timestamptz,
    used_at            timestamptz,
    used_decision_id   uuid,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL
);
-- The same action asked again while its request is open gets that request.
CREATE UNIQUE INDEX approval_request_open ON approval_request (organization_id, project_id, action_hash)
    WHERE status = 'PENDING';
CREATE UNIQUE INDEX approval_request_token ON approval_request (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX approval_request_recent ON approval_request (organization_id, project_id, created_at DESC, id DESC);

-- A request only moves forward (PENDING → APPROVED | DENIED | EXPIRED,
-- APPROVED → USED | EXPIRED); a closed one never changes; what was asked
-- never changes.
CREATE FUNCTION approval_request_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('DENIED', 'EXPIRED', 'USED')
       OR (OLD.status <> NEW.status AND NOT (
            (OLD.status = 'PENDING' AND NEW.status IN ('APPROVED', 'DENIED', 'EXPIRED'))
         OR (OLD.status = 'APPROVED' AND NEW.status IN ('USED', 'EXPIRED'))))
       OR (NEW.id, NEW.organization_id, NEW.project_id, NEW.tool, NEW.risk, NEW.agent, NEW.agent_version,
           NEW.environment, NEW.trace_id, NEW.action_hash, NEW.arguments, NEW.summary, NEW.policy_name,
           NEW.policy_version_id, NEW.rule, NEW.reason, NEW.decision_id, NEW.requested_by, NEW.expires_at,
           NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.organization_id, OLD.project_id, OLD.tool, OLD.risk, OLD.agent, OLD.agent_version,
           OLD.environment, OLD.trace_id, OLD.action_hash, OLD.arguments, OLD.summary, OLD.policy_name,
           OLD.policy_version_id, OLD.rule, OLD.reason, OLD.decision_id, OLD.requested_by, OLD.expires_at,
           OLD.created_at) THEN
        RAISE EXCEPTION 'approval_request % cannot change that way (% → %)', OLD.id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER approval_request_forward BEFORE UPDATE ON approval_request
    FOR EACH ROW EXECUTE FUNCTION approval_request_guard();
CREATE TRIGGER approval_request_kept BEFORE DELETE ON approval_request
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Every call that presented an approval's token, with what it asked for.
CREATE TABLE approval_attempt (
    id               uuid PRIMARY KEY,
    approval_id      uuid NOT NULL REFERENCES approval_request (id),
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    result           text NOT NULL CHECK (result IN ('executed', 'mismatch', 'used', 'expired', 'not_approved')),
    action_hash      text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    arguments        jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
    subject          text NOT NULL,
    trace_id         text,
    decision_id      uuid,
    created_at       timestamptz NOT NULL
);
CREATE INDEX approval_attempt_of ON approval_attempt (approval_id, created_at);
CREATE TRIGGER approval_attempt_append_only BEFORE UPDATE OR DELETE ON approval_attempt
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- What an idempotency key did, per project and tool.
CREATE TABLE idempotency_record (
    organization_id        uuid NOT NULL,
    project_id             uuid NOT NULL,
    tool                   text NOT NULL,
    key                    text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    action_hash            text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    state                  text NOT NULL CHECK (state IN ('IN_PROGRESS', 'COMPLETED', 'UNKNOWN')),
    response_status        int,
    response_body          bytea,
    response_content_type  text,
    decision_id            uuid NOT NULL,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    expires_at             timestamptz NOT NULL,
    PRIMARY KEY (organization_id, project_id, tool, key)
);
CREATE INDEX idempotency_record_expiry ON idempotency_record (expires_at);

-- The distinct actions of a tool forwarded in a trace (trace.calls counts
-- the other ones: a retry of the same action is not another call).
CREATE TABLE trace_tool_call (
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    trace_id         text NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    tool             text NOT NULL,
    action_hash      text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    first_at         timestamptz NOT NULL,
    PRIMARY KEY (organization_id, project_id, trace_id, tool, action_hash)
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
