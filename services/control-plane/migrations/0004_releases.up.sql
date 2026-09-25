-- Releases (spec §28, §38): a candidate version of an agent against its
-- baseline, with the change set that says what changed. Each evaluation of a
-- release is a revision holding what it rested on when it was requested (the
-- gate policy, the change's impact, the suite pinned from it, the candidate's
-- tools) and, once the evaluation service answered, the gate's decision.
-- Evidence is immutable once written (spec §91): a new evaluation is a new
-- revision. An override records who let a gated release through and why; it
-- never changes the decision (spec §92).

-- Composite keys so a release's evaluations, decisions and overrides belong
-- to its organization and project: the invariants the use cases check are
-- also the schema's.
ALTER TABLE change_set ADD CONSTRAINT change_set_scope_key UNIQUE (organization_id, project_id, agent_id, id);

CREATE TABLE release (
    id                   uuid PRIMARY KEY,
    organization_id      uuid NOT NULL,
    project_id           uuid NOT NULL,
    agent_id             uuid NOT NULL,
    change_set_id        uuid NOT NULL,
    baseline_version_id  uuid NOT NULL,
    candidate_version_id uuid NOT NULL,
    title                text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    commit_sha           text CHECK (commit_sha ~ '^[a-f0-9]{7,64}$'),
    ci_url               text CHECK (ci_url ~ '^https?://' AND length(ci_url) <= 2048),
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (baseline_version_id <> candidate_version_id),
    UNIQUE (organization_id, project_id, id),
    FOREIGN KEY (organization_id, project_id, agent_id, change_set_id)
        REFERENCES change_set(organization_id, project_id, agent_id, id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id, baseline_version_id) REFERENCES agent_version(agent_id, id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id, candidate_version_id) REFERENCES agent_version(agent_id, id) ON DELETE CASCADE
);
CREATE INDEX release_page ON release (organization_id, project_id, created_at DESC, id DESC);
CREATE INDEX release_agent_page ON release (organization_id, agent_id, created_at DESC, id DESC);
CREATE TRIGGER release_immutable BEFORE UPDATE ON release
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE release_evaluation (
    id              uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    release_id      uuid NOT NULL,
    revision        int NOT NULL CHECK (revision > 0),
    status          text NOT NULL CHECK (status IN ('EVALUATING', 'DECIDED')),
    requested_by    text NOT NULL,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    -- What the evaluation rests on, as it was when requested.
    policy          jsonb NOT NULL,
    impact          jsonb NOT NULL,
    suite           jsonb NOT NULL,
    tools           jsonb NOT NULL,
    -- The evaluation service's run, once it answered (none when nothing ran).
    eval_run_id     uuid,
    decided_at      timestamptz,
    CHECK ((status = 'DECIDED') = (decided_at IS NOT NULL)),
    UNIQUE (release_id, revision),
    UNIQUE (organization_id, project_id, release_id, id),
    FOREIGN KEY (organization_id, project_id, release_id)
        REFERENCES release(organization_id, project_id, id) ON DELETE CASCADE
);
CREATE INDEX release_evaluation_run ON release_evaluation (eval_run_id) WHERE eval_run_id IS NOT NULL;

-- A revision only moves once, from EVALUATING to DECIDED, recording its run;
-- what it rests on never changes.
CREATE FUNCTION release_evaluation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'EVALUATING' OR NEW.status <> 'DECIDED'
       OR (NEW.id, NEW.organization_id, NEW.project_id, NEW.release_id, NEW.revision, NEW.requested_by,
           NEW.requested_at, NEW.policy, NEW.impact, NEW.suite, NEW.tools)
          IS DISTINCT FROM
          (OLD.id, OLD.organization_id, OLD.project_id, OLD.release_id, OLD.revision, OLD.requested_by,
           OLD.requested_at, OLD.policy, OLD.impact, OLD.suite, OLD.tools)
       OR (OLD.eval_run_id IS NOT NULL AND NEW.eval_run_id IS DISTINCT FROM OLD.eval_run_id) THEN
        RAISE EXCEPTION 'release_evaluation % is evidence: it only moves from EVALUATING to DECIDED', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER release_evaluation_once BEFORE UPDATE ON release_evaluation
    FOR EACH ROW EXECUTE FUNCTION release_evaluation_guard();

CREATE TABLE gate_decision (
    id                    uuid PRIMARY KEY,
    organization_id       uuid NOT NULL,
    project_id            uuid NOT NULL,
    release_id            uuid NOT NULL,
    release_evaluation_id uuid NOT NULL UNIQUE,
    outcome               text NOT NULL CHECK (outcome IN ('PASS', 'WARN', 'BLOCK')),
    incomplete            boolean NOT NULL,
    rules_version         text NOT NULL,
    risk_index            int NOT NULL CHECK (risk_index BETWEEN 0 AND 100),
    -- The decision (rules with their evidence, counts, coverage, risk index)
    -- and everything it rests on, hashed together.
    decision              jsonb NOT NULL,
    input                 jsonb NOT NULL,
    -- What the list shows without reading the decision.
    summary               jsonb NOT NULL,
    evidence_sha256       text NOT NULL CHECK (evidence_sha256 ~ '^[a-f0-9]{64}$'),
    decided_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, project_id, release_id, id),
    FOREIGN KEY (organization_id, project_id, release_id, release_evaluation_id)
        REFERENCES release_evaluation(organization_id, project_id, release_id, id) ON DELETE CASCADE
);
CREATE TRIGGER gate_decision_immutable BEFORE UPDATE ON gate_decision
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE gate_override (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    release_id       uuid NOT NULL,
    -- One override per decision: a new evaluation needs its own.
    gate_decision_id uuid NOT NULL UNIQUE,
    original_outcome text NOT NULL CHECK (original_outcome IN ('WARN', 'BLOCK')),
    reason           text NOT NULL CHECK (length(reason) BETWEEN 10 AND 2000),
    ticket_url       text CHECK (ticket_url ~ '^https?://' AND length(ticket_url) <= 2048),
    expires_at       timestamptz,
    actor            text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    FOREIGN KEY (organization_id, project_id, release_id, gate_decision_id)
        REFERENCES gate_decision(organization_id, project_id, release_id, id) ON DELETE CASCADE
);
CREATE TRIGGER gate_override_immutable BEFORE UPDATE ON gate_override
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
